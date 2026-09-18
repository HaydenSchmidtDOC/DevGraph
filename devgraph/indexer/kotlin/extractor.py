"""Tree-sitter-based Kotlin source-code extractor.

Mirrors `devgraph/indexer/java/extractor.py`'s shape and philosophy — same
GraphNode/GraphRelationship/ExtractionResult dataclasses (shared via
`devgraph.indexer.common`), same name-based CALLS resolution, same
non-materializing-guess approach to IMPORTS. Extracts:

  - Module (the file itself)
  - Class (Kotlin `class`/`interface`/`enum class`/`sealed class`/`data
    class` are all one `class_declaration` node kind distinguished only by a
    `class_modifier`; top-level `object` declarations map to Class too, same
    way Java folds interface/enum into Class), with supertypes
    (`delegation_specifiers`) -> EXTENDS, and CONTAINS
  - Function (`fun` declarations — both top-level and member functions), with
    CONTAINS
  - KDoc (`/** ... */`) immediately preceding a class/function, stored the
    same way Java's Javadoc is (`description`/`docstring_full`)
  - `import` statements, resolved via Kotlin's package/folder convention
    (mirrors Java's `_detect_source_root`: `package com.motonav.app.ride` in
    a folder that mirrors `com/motonav/app/ride`)

Grammar routing is by file extension: only `.kt` source files are routed to
this extractor. `.kts` (Gradle build scripts) is intentionally NOT handled —
build config, not source — per the plan.

Known limitations (v1 scope, structural parity — documented, not to be
"fixed" later):
  - `data`/`sealed`/`enum`/`interface` classes, top-level `object`, and
    `companion object` are all mapped to the single Class label; their
    distinguishing modifier is recorded in the `modifiers` property rather
    than modeled as separate node labels (DevGraph's schema has no
    DataClass/SealedClass/Enum/Object labels).
  - `companion object` declarations are treated as Class nodes when they
    carry an identifier; an anonymous `companion object { ... }` is skipped
    (no name to key a node on).
  - Constructors (`primary_constructor`, `secondary_constructor`) are NOT
    extracted as Function nodes — Kotlin's primary constructor is part of the
    class header, not a named `fun`, and secondary constructors are uncommon.
    This differs from Java, whose constructors share the class name and are
    extracted; documented as an accepted asymmetry.
  - Extension functions (`fun String.foo()`) are extracted as plain Function
    nodes keyed on the function's own name (`foo`), not on the receiver type.
  - `suspend`/`inline`/`operator`/`infix`/`tailrec` modifiers are recorded in
    `decorators` only when they appear as annotations; Kotlin's own
    `function_modifier` keyword nodes are not surfaced.
  - IMPORTS resolution: same package/folder-convention guess as Java — a
    guessed target that doesn't match a real indexed file simply never
    produces an edge (`upsert_relationship` only MATCH-links real nodes).
    Wildcard imports (`import a.b.*`) get the bare dotted package name but no
    file-path guess. No `import` alias (`import ... as x`) handling.
  - **Top-level symbol imports do not resolve to a file** (Kotlin-specific,
    more than Java): `import com.motonav.app.nav.forwardRouteGeometry` guesses
    `.../nav/forwardRouteGeometry.kt`, but that function actually lives in
    `RouteGeometry.kt` — Kotlin freely puts multiple top-level functions/
    values per file, and the import names the *symbol*, not the file. Java's
    1-public-class-per-file convention makes its import guess near-perfect;
    Kotlin has no such convention, so top-level-symbol imports (a large share
    of idiomatic Kotlin) are best-effort and frequently don't materialize an
    edge. Class/interface/object imports *do* resolve (their file is usually
    named for the type). Accepted for v1 — the non-materializing-guess
    approach means these simply produce no edge rather than a wrong one.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_kotlin as tskotlin
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

logger = logging.getLogger(__name__)

_KOTLIN_LANGUAGE = Language(tskotlin.language())

# Kotlin folds class/interface/enum/sealed/data into one `class_declaration`
# node kind; `object` and `companion object` are separate kinds. All map to
# the graph's single Class label (see module docstring).
_TYPE_DECL_TYPES = ("class_declaration", "object_declaration", "companion_object")
_FUNCTION_TYPES = ("function_declaration",)


def _make_parser() -> Parser:
    return Parser(_KOTLIN_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _child_of_type(node: Node, type_name: str) -> Node | None:
    """Find the first direct child of the given type."""
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _simple_type_name(node: Node, source: bytes) -> str:
    """Render a `user_type` node down to the simple class/interface name
    Class nodes are keyed on, stripping generic arguments and any package
    qualifier.

    `List<LocalOffsetMeters>` -> 'List', `TripState.Idle` -> 'Idle',
    `com.example.Foo` -> 'Foo'.
    """
    # user_type may carry type_arguments (generics) and a package-qualified
    # scoped/qualified_identifier. Keep only the type identifier's own name.
    # The grammar's user_type isn't field-named; the leading identifier or
    # qualified path is what we want, before any type_arguments child.
    head = None
    for child in node.named_children:
        if child.type in ("identifier", "qualified_identifier", "navigation_expression"):
            head = _text(child, source)
            break
    if head is None:
        head = _text(node, source)
    # Strip generic args: 'List<...>' -> 'List'
    head = head.split("<", 1)[0]
    # Strip trailing navigation ('.Idle' -> 'Idle') and package prefix.
    return head.rsplit(".", 1)[-1]


def _extract_supertype_names(node: Node, source: bytes) -> list[str]:
    """Supertypes from a `class_declaration`'s `delegation_specifiers`
    child (Kotlin's `: B(), C` — both interfaces and the superclass land
    here). Each `delegation_specifier` wraps a `user_type` (direct) or a
    `constructor_invocation` (which itself contains the `user_type`)."""
    specifiers = _child_of_type(node, "delegation_specifiers")
    if specifiers is None:
        return []
    names: list[str] = []
    for child in specifiers.named_children:
        if child.type != "delegation_specifier":
            continue
        user_type = _child_of_type(child, "user_type")
        if user_type is None:
            # constructor_invocation wraps the type: `B()` -> B
            invocation = _child_of_type(child, "constructor_invocation")
            if invocation is not None:
                user_type = _child_of_type(invocation, "user_type")
        if user_type is not None:
            names.append(_simple_type_name(user_type, source))
    return names


def _extract_annotation_names(node: Node, source: bytes) -> list[str]:
    """Annotation names (`@Composable` -> 'Composable') from a declaration's
    `modifiers` child, filling the same node-property slot Java's
    annotations use (`decorators`)."""
    modifiers = _child_of_type(node, "modifiers")
    if modifiers is None:
        return []
    names = []
    for child in modifiers.named_children:
        if child.type == "annotation":
            raw = _text(child, source).lstrip("@")
            # Strip any `(...)` argument list: '@Ann(x)' -> 'Ann'
            names.append(raw.split("(", 1)[0])
    return names


def _clean_kdoc(raw: str) -> str:
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
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """First line/paragraph, truncated with an ellipsis if long (same as the
    Java/Python extractors)."""
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _preceding_kdoc(node: Node, prev_sibling: Node | None, source: bytes) -> str | None:
    """A `block_comment` immediately preceding `node`, if it looks like KDoc
    (`/** ... */`, not just `/* ... */`)."""
    if prev_sibling is None or prev_sibling.type != "block_comment":
        return None
    raw = _text(prev_sibling, source)
    if not raw.strip().startswith("/**"):
        return None
    return _clean_kdoc(raw)


def _collect_identifiers(node: Node, source: bytes) -> list[str]:
    """All `identifier` descendant texts, in source order. Used to pick the
    last identifier of a `navigation_expression` as the callee's simple name
    (`System.currentTimeMillis` -> 'currentTimeMillis', `obj.foo` -> 'foo')."""
    ids: list[str] = []
    for child in node.children:
        if child.type == "identifier":
            ids.append(_text(child, source))
        else:
            ids.extend(_collect_identifiers(child, source))
    return ids


def _callee_simple_name(invocation: Node, source: bytes) -> str | None:
    """A `call_expression` node's callee name.

    A Kotlin `call_expression`'s first named child is either a bare
    `identifier` (`foo()`), or a `navigation_expression` for a qualified
    call (`obj.foo()`, `System.currentTimeMillis()`,
    `trip.x.toInt()`). For the qualified case the callee's simple name is the
    *last* identifier in the navigation path. Same over-linking philosophy as
    Java/Python: `obj.foo()` and `foo()` both target 'foo'.
    """
    for child in invocation.named_children:
        if child.type == "value_arguments":
            continue  # skip the argument list
        if child.type == "identifier":
            return _text(child, source)
        if child.type == "navigation_expression":
            ids = _collect_identifiers(child, source)
            return ids[-1] if ids else None
    return None


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a function body (or any statement) for `call_expression`s and
    return the callee's simple name, not descending into nested function/
    class/object declarations — those are walked separately so calls are
    attributed to the correct enclosing scope."""
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in (*_TYPE_DECL_TYPES, *_FUNCTION_TYPES):
            return  # nested scope — attributed separately by the caller
        if node.type == "call_expression":
            name = _callee_simple_name(node, source)
            if name:
                targets.append(name)
        for child in node.children:
            walk(child)

    walk(body)
    return targets


def _package_name(root: Node, source: bytes) -> str | None:
    """The file's `package` declaration, e.g. 'com.motonav.app.ride'."""
    for child in root.named_children:
        if child.type == "package_header":
            text = _text(child, source).strip()
            if text.startswith("package "):
                return text[len("package ") :].strip()
            return text
    return None


def _detect_source_root(file_path: str, package_name: str | None) -> str:
    """Guess the source-root prefix in front of a package's folder structure
    (same logic as Java's `_detect_source_root`)."""
    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    if not package_name:
        return current_dir
    pkg_path = package_name.replace(".", "/")
    if current_dir == pkg_path:
        return ""
    if current_dir.endswith("/" + pkg_path):
        return current_dir[: -(len(pkg_path) + 1)]
    return ""


def _import_file_guess(source_root: str, dotted: str) -> str | None:
    """Reinterpret an import's dotted path as a same-repo `.kt` file guess.

    'import com.motonav.app.nav.BucketedManeuver' ->
    'app/src/main/java/com/motonav/app/nav/BucketedManeuver.kt'
    (source_root='app/src/main/java'). The imported symbol is assumed to be
    the dotted path's last segment — the class/object/function name. Same
    non-materializing-guess philosophy as Java: a wrong guess just never
    produces an edge.
    """
    segments = dotted.split(".")
    if not segments or not segments[-1]:
        return None
    pkg_segments, name = segments[:-1], segments[-1]
    rel = "/".join([*pkg_segments, name]) + ".kt"
    return f"{source_root}/{rel}" if source_root else rel


def _extract_imports(root: Node, source: bytes) -> list[tuple[str, bool]]:
    """Extract `import` declarations, returning (dotted_path, is_wildcard).

    Only `import` nodes carrying a `qualified_identifier` child are real
    imports (the grammar also emits a bare `import` keyword node). A wildcard
    import (`import a.b.*`) has its trailing `.*` stripped and `is_wildcard`
    set so the caller skips the file-path guess.
    """
    imports: list[tuple[str, bool]] = []
    for child in root.named_children:
        if child.type != "import":
            continue
        path_node = _child_of_type(child, "qualified_identifier")
        if path_node is None:
            continue
        dotted = _text(path_node, source)
        # A wildcard import carries a bare `*` token as a separate child of
        # the `import` node (not inside the qualified_identifier): `import
        # com.example.util.*` -> qualified_identifier 'com.example.util' +
        # '*' token.
        is_wildcard = any(c.type == "*" for c in child.children)
        imports.append((dotted, is_wildcard))
    return imports


def extract_kotlin_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Kotlin file and extract nodes and relationships.

    Args:
        source_code: The Kotlin source as a string.
        file_path: The Module node's identity — the file's path relative to
            the repo root, forward-slashed. Also used to detect the
            source-root prefix for import resolution.
        repo_id: Repository ID for scoping nodes.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error Tree-sitter still returns a best-effort tree, so partial
        results are extracted and the error logged rather than the whole file
        being skipped.
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

    for dotted, is_wildcard in _extract_imports(root, source_bytes):
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
            continue
        file_guess = _import_file_guess(source_root, dotted)
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

    def _visit_type_decl(node: Node, parent_name: str | None, parent_label: str, prev_sibling: Node | None) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            # `companion object { ... }` has no name field; fall back to an
            # explicit `identifier` child, else skip (anonymous companion).
            name_node = _child_of_type(node, "identifier")
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
        kdoc = _preceding_kdoc(node, prev_sibling, source_bytes)
        if kdoc:
            class_properties["description"] = _docstring_summary(kdoc)
            class_properties["docstring_full"] = kdoc

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
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
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
        if body_node is None:
            # enum bodies and companion/object bodies aren't field-named
            # `body` — look for a class_body/enum_class_body child instead.
            body_node = _child_of_type(node, "class_body") or _child_of_type(node, "enum_class_body")
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
        kdoc = _preceding_kdoc(node, prev_sibling, source_bytes)
        if kdoc:
            func_properties["description"] = _docstring_summary(kdoc)
            func_properties["docstring_full"] = kdoc

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
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
            )
        )

        # Kotlin's grammar has no `body` field name — a function's body is a
        # `function_body` child (the `name` field exists, `body` does not).
        body_node = node.child_by_field_name("body") or _child_of_type(node, "function_body")
        if body_node is not None:
            caller_class = parent_name if parent_label == "Class" else None
            for target in _extract_call_targets(body_node, source_bytes):
                _emit_call(func_name, "Function", target, caller_class)

            # Nested function declarations (a `fun` declared inside this
            # function's body) are visited with this function as their parent,
            # so their own calls are attributed to them rather than hoisted
            # here — `_extract_call_targets` stops descending at nested
            # function/class scopes, mirroring Python's nested-function walk.
            for child in body_node.named_children:
                if child.type == "function_declaration":
                    _visit_function(child, func_name, "Function", prev_sibling=None)
                elif child.type == "block":
                    for grandchild in child.named_children:
                        if grandchild.type == "function_declaration":
                            _visit_function(grandchild, func_name, "Function", prev_sibling=None)

    def visit_block(block: Node, parent_name: str | None, parent_label: str) -> None:
        """Visit declarations in a block (module/class/object body)."""
        prev_sibling: Node | None = None
        for node in block.named_children:
            if node.type in _TYPE_DECL_TYPES:
                _visit_type_decl(node, parent_name, parent_label, prev_sibling)
            elif node.type in _FUNCTION_TYPES:
                _visit_function(node, parent_name, parent_label, prev_sibling)
            elif node.type == "block_comment":
                pass  # only relevant as a *following* node's kdoc
            else:
                # Property/initializer/statement: not a def, but may contain
                # calls — attribute those to the enclosing scope.
                caller_class = parent_name if parent_label == "Class" else None
                for target in _extract_call_targets(node, source_bytes):
                    _emit_call(parent_name if parent_name else file_path, parent_label, target, caller_class)
            prev_sibling = node

    visit_block(root, None, "Module")

    return result


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a Kotlin file and upsert results into the graph.

    Thin wrapper around extract_kotlin_file(), matching Java's index_file()
    signature/behavior: when `repo_root` is given the Module node is keyed by
    the file's path relative to repo_root (forward slashes); when omitted,
    falls back to the bare filename.
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

    result = extract_kotlin_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
