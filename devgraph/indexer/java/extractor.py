"""Tree-sitter-based Java source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape and philosophy —
same GraphNode/GraphRelationship/ExtractionResult dataclasses (shared via
`devgraph.indexer.common`), same name-based CALLS resolution, same
non-materializing-guess approach to IMPORTS. Extracts:

  - Module (the file itself)
  - Class/interface/enum declarations, with `extends`/`implements` -> EXTENDS
  - Methods and constructors (Java has no free functions — every method
    belongs to a class/interface/enum, same situation as C#), with CONTAINS
  - Javadoc (`/** ... */`) immediately preceding a class/method/constructor,
    stored the same way Python's docstring is (`description`/`docstring_full`)
  - `import` statements, resolved via Java's package/folder convention
    (see `_extract_imports` and `_detect_source_root`) — the cleanest of the
    six languages in Implementation Plan #8, since `package a.b.c;` living
    in a folder that mirrors `a/b/c` is close to a hard convention, not just
    a common style.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

logger = logging.getLogger(__name__)

_JAVA_LANGUAGE = Language(tsjava.language())

# class_declaration, interface_declaration, and enum_declaration are all
# treated as "Class" nodes (Java has no separate interface/enum label in
# the graph schema, matching how the generic Class/Function/Module labels
# are meant to generalize across languages).
_TYPE_DECL_TYPES = ("class_declaration", "interface_declaration", "enum_declaration")
_METHOD_DECL_TYPES = ("method_declaration", "constructor_declaration")


def _make_parser() -> Parser:
    return Parser(_JAVA_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _child_of_type(node: Node, type_name: str) -> Node | None:
    """Find the first direct child of the given type.

    Several fields we need (interface `extends`, class `implements`) don't
    have a stable field name across node kinds in the Java grammar (e.g.
    `extends_interfaces` on `interface_declaration` has no field name at
    all — verified against tree-sitter-java 0.23), so scanning children by
    type is more reliable than `child_by_field_name` for those.
    """
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _simple_type_name(node: Node, source: bytes) -> str:
    """Render a type node (possibly generic, possibly dotted) down to the
    simple class/interface name Class nodes are actually keyed on.

    `Base<String>` -> 'Base', `java.util.List<String>` -> 'List',
    `pkg.Base` -> 'Base'. Class/interface/enum nodes in this extractor (and
    Python's) are keyed by simple name, not a fully-qualified one, so an
    EXTENDS/IMPLEMENTS target must match that same convention to have any
    chance of resolving to a real node.
    """
    if node.type == "generic_type":
        return _simple_type_name(node.children[0], source)
    text = _text(node, source)
    return text.rsplit(".", 1)[-1]


def _extract_supertype_names(node: Node, source: bytes) -> list[str]:
    """Base class + implemented/extended interfaces for a type declaration,
    handling class_declaration (`superclass` + `super_interfaces`),
    interface_declaration (`extends_interfaces`, can list several), and
    enum_declaration (`super_interfaces` only)."""
    names: list[str] = []

    superclass = _child_of_type(node, "superclass")
    if superclass is not None and superclass.named_children:
        names.append(_simple_type_name(superclass.named_children[0], source))

    for wrapper_type in ("super_interfaces", "extends_interfaces"):
        wrapper = _child_of_type(node, wrapper_type)
        if wrapper is None:
            continue
        type_list = wrapper.named_children[0] if wrapper.named_children else None
        if type_list is None:
            continue
        for type_node in type_list.named_children:
            names.append(_simple_type_name(type_node, source))

    return names


def _extract_annotation_names(node: Node, source: bytes) -> list[str]:
    """Annotation names (`@Override` -> 'Override') from a declaration's
    `modifiers` child, filling the same node-property slot Python's
    `decorators` list occupies — Java annotations are the closest
    equivalent convention."""
    modifiers = _child_of_type(node, "modifiers")
    if modifiers is None:
        return []
    names = []
    for child in modifiers.named_children:
        if child.type in ("marker_annotation", "annotation"):
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(name_node, source))
    return names


def _clean_javadoc(raw: str) -> str:
    """Strip `/** ... */` delimiters and each line's leading `*`."""
    text = raw.strip()
    if text.startswith("/**"):
        text = text[3:]
    elif text.startswith("/*"):
        text = text[2:]
    if text.endswith("*/"):
        text = text[:-2]

    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("*"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    # Drop leading/trailing blank lines produced by the /** and */ markers
    # sitting on their own lines.
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """Same summary convention as the Python extractor: first
    line/paragraph, truncated with an ellipsis if it's long."""
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _preceding_javadoc(node: Node, prev_sibling: Node | None, source: bytes) -> str | None:
    """A `block_comment` immediately preceding `node` in its parent's child
    list, if it looks like Javadoc (`/** ... */`, not just `/* ... */`)."""
    if prev_sibling is None or prev_sibling.type != "block_comment":
        return None
    raw = _text(prev_sibling, source)
    if not raw.strip().startswith("/**"):
        return None
    return _clean_javadoc(raw)


def _callee_simple_name(invocation: Node, source: bytes) -> str | None:
    """A `method_invocation` node's callee name.

    Unlike Python's grammar, `method_invocation` carries a `name` field
    directly regardless of whether it's a bare call (`foo()`), a
    qualified one (`obj.foo()`/`this.foo()`), or a chained one
    (`getHandler().invoke()`) — no recursion into the `object` field is
    needed to get the simple callee name. Same over-linking philosophy as
    Python's `_callee_simple_name`: 'obj.foo()' and a bare 'foo()' both
    target the Function node named 'foo', since there's no type info to
    disambiguate further.
    """
    name_node = invocation.child_by_field_name("name")
    if name_node is None:
        return None
    return _text(name_node, source)


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a method/constructor body (or any statement) for
    `method_invocation`s and return the callee's simple name, not
    descending into nested type/method declarations — those are walked
    separately so calls are attributed to the correct enclosing scope."""
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in (*_TYPE_DECL_TYPES, *_METHOD_DECL_TYPES):
            return  # nested scope — attributed separately by the caller
        if node.type == "method_invocation":
            name = _callee_simple_name(node, source)
            if name:
                targets.append(name)
        for child in node.children:
            walk(child)

    walk(body)
    return targets


def _package_name(root: Node, source: bytes) -> str | None:
    for child in root.named_children:
        if child.type == "package_declaration":
            # package_declaration := 'package' (scoped_identifier | identifier) ';'
            for c in child.named_children:
                if c.type in ("scoped_identifier", "identifier"):
                    return _text(c, source)
    return None


def _detect_source_root(file_path: str, package_name: str | None) -> str:
    """Guess the source-root prefix (e.g. 'src/main/java') that sits in
    front of a package's folder structure, by checking whether this file's
    own directory ends with its own `package` declaration reinterpreted as
    a path.

    'src/main/java/com/example/foo/Bar.java' declaring 'package
    com.example.foo;' -> source root 'src/main/java'. A file with no
    package declaration (default package) is its own source root. A file
    whose directory does *not* end with its declared package path (a real
    possibility DevGraph can't rule out — misconfigured build, generated
    code, a file living outside its declared package's expected location)
    falls back to '' (repo-root-relative guesses), the same
    doesn't-crash-just-doesn't-resolve behavior Python's dotted-import
    guess has when it's wrong.
    """
    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    if not package_name:
        return current_dir
    pkg_path = package_name.replace(".", "/")
    if current_dir == pkg_path:
        return ""
    if current_dir.endswith("/" + pkg_path):
        return current_dir[: -(len(pkg_path) + 1)]
    return ""


def _import_file_guess(source_root: str, dotted: str, is_static: bool) -> str | None:
    """Reinterpret an import's dotted path as a same-repo file path guess,
    using the detected source-root prefix.

    'import com.example.bar.Baz;' -> 'src/main/java/com/example/bar/Baz.java'
    (source_root='src/main/java'). The class name is assumed to be the
    dotted path's last segment for a normal import ('import a.b.C;' -> class
    'C'), or its second-to-last segment for a static import ('import static
    a.b.C.member;' -> class 'C', member 'member') — Java's own ambiguity
    between a package segment and an inner-class segment in the middle of a
    dotted path is not resolved here (documented limitation, same spirit as
    Python's guess-that-may-not-materialize approach: `upsert_relationship`
    only MATCHes real existing nodes, so a wrong guess just never produces
    an edge).
    """
    segments = dotted.split(".")
    if is_static:
        segments = segments[:-1]  # drop the imported static member name
    if not segments:
        return None
    pkg_segments, class_name = segments[:-1], segments[-1]
    if not class_name:
        return None
    rel = "/".join([*pkg_segments, class_name]) + ".java"
    return f"{source_root}/{rel}" if source_root else rel


def _extract_imports(root: Node, source: bytes) -> list[tuple[str, bool, bool]]:
    """Extract `import` declarations from the parse tree.

    Returns a list of (dotted_path, is_static, is_wildcard) tuples.
    `dotted_path` excludes the trailing `.*` for a wildcard import.
    """
    imports: list[tuple[str, bool, bool]] = []
    for child in root.named_children:
        if child.type != "import_declaration":
            continue
        is_static = any(c.type == "static" for c in child.children)
        is_wildcard = any(c.type == "asterisk" for c in child.children)
        path_node = next((c for c in child.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if path_node is None:
            continue
        imports.append((_text(path_node, source), is_static, is_wildcard))
    return imports


def extract_java_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Java file and extract nodes and relationships.

    Args:
        source_code: The Java source as a string.
        file_path: The Module node's identity — the file's path relative
            to the repo root, forward-slashed (e.g.
            'src/main/java/com/example/foo/Bar.java'). Also used to detect
            the source-root prefix for import resolution (see
            `_detect_source_root`).
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (ERROR
        nodes, not an exception), so partial results are extracted and the
        error is logged rather than the whole file being skipped.
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

    package_name = _package_name(root, source_bytes)
    source_root = _detect_source_root(file_path, package_name)

    for dotted, is_static, is_wildcard in _extract_imports(root, source_bytes):
        # Bare dotted form, kept for compatibility/introspection, same as
        # Python's bare-dotted-name target.
        result.relationships.append(
            GraphRelationship(
                from_label="Module",
                from_name=file_path,
                rel_type="IMPORTS",
                to_label="Module",
                to_name=dotted,
                repo_id=repo_id,
            )
        )
        if is_wildcard:
            # A wildcard import ('import a.b.*;') names a package, not a
            # single file — there's no single Module a wildcard import
            # could resolve to (a Java package isn't itself a Module node
            # in this schema), so no file-path guess is made for it.
            continue
        file_guess = _import_file_guess(source_root, dotted, is_static)
        if file_guess:
            result.relationships.append(
                GraphRelationship(
                    from_label="Module",
                    from_name=file_path,
                    rel_type="IMPORTS",
                    to_label="Module",
                    to_name=file_guess,
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
        """Visit statements in a block (module/class/interface/enum body)."""
        prev_sibling: Node | None = None
        for node in block.named_children:
            if node.type in _TYPE_DECL_TYPES:
                _visit_class(node, parent_name, parent_label, prev_sibling)
            elif node.type in _METHOD_DECL_TYPES:
                _visit_function(node, parent_name, parent_label, prev_sibling)
            elif node.type == "block_comment":
                pass  # only relevant as a *following* node's javadoc, handled via prev_sibling
            else:
                # Field declarations, static/instance initializers, enum
                # constants, etc: not a def, but may still contain calls
                # (e.g. a field initializer) — attribute those to the
                # enclosing scope, mirroring Python's else-branch handling
                # of module/class-body-level statements.
                caller_class = parent_name if parent_label == "Class" else None
                for target in _extract_call_targets(node, source_bytes):
                    _emit_call(parent_name if parent_name else file_path, parent_label, target, caller_class)
            prev_sibling = node

    def _visit_class(node: Node, parent_name: str | None, parent_label: str, prev_sibling: Node | None) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        class_properties: dict = {
            "type": "class",
            "decorators": _extract_annotation_names(node, source_bytes),
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        javadoc = _preceding_javadoc(node, prev_sibling, source_bytes)
        if javadoc:
            class_properties["description"] = _docstring_summary(javadoc)
            class_properties["docstring_full"] = javadoc

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

        for supertype in _extract_supertype_names(node, source_bytes):
            result.relationships.append(
                GraphRelationship(
                    from_label="Class",
                    from_name=class_name,
                    rel_type="EXTENDS",
                    to_label="Class",
                    to_name=supertype,
                    repo_id=repo_id,
                )
            )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            visit_block(body_node, class_name, "Class")

    def _visit_function(node: Node, parent_name: str | None, parent_label: str, prev_sibling: Node | None) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        func_properties: dict = {
            "type": "function",
            "decorators": _extract_annotation_names(node, source_bytes),
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        javadoc = _preceding_javadoc(node, prev_sibling, source_bytes)
        if javadoc:
            func_properties["description"] = _docstring_summary(javadoc)
            func_properties["docstring_full"] = javadoc

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

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            caller_class = parent_name if parent_label == "Class" else None
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(func_name, "Function", target, caller_class)

    visit_block(root, None, "Module")

    return result


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a Java file and upsert results into the graph.

    Thin wrapper around extract_java_file(), matching
    `python/extractor.py`'s index_file() signature/behavior: when
    `repo_root` is given the Module node is keyed by the file's path
    relative to repo_root (forward slashes); when omitted, falls back to
    the bare filename.

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

    result = extract_java_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
