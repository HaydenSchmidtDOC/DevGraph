"""Tree-sitter-based Kotlin source-code extractor.

Mirrors `devgraph/indexer/java/extractor.py`'s shape and philosophy — same
GraphNode/GraphRelationship/ExtractionResult dataclasses (shared via
`devgraph.indexer.common`), the same path-pin resolution of CALLS and
IMPORTS (see the limitations below for Kotlin's own shapes). Extracts:

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
    (`jvm.JvmLayout`, as Java: `package com.motonav.app.ride` in a folder
    that mirrors `com/motonav/app/ride`, or its tail)

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
  - IMPORTS and CALLS resolve to path pins (`_KotlinResolver`; see the
    Java and Kotlin section of
    docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md):
    the source root may leave the common root package out of the tree
    (`jvm.JvmLayout`); an import of a class or top-level function is an
    IMPORTS edge to that symbol in its package's directory (a Kotlin file
    can hold several, named after none of them); a wildcard, to the
    package's files.
"""

from __future__ import annotations

import bisect
import logging
import posixpath
import re
from pathlib import Path

import tree_sitter_kotlin as tskotlin
from tree_sitter import Language, Node, Parser, Range

from devgraph.indexer.calls import (
    STOP_METHODS,
    STOP_TYPES,
    CallSites,
    call_rows,
    import_rows,
)
from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    own_edges,
)
from devgraph.indexer.jvm import JvmLayout, type_segment

logger = logging.getLogger(__name__)

_KOTLIN_LANGUAGE = Language(tskotlin.language())

# Kotlin folds class/interface/enum/sealed/data into one `class_declaration`
# node kind; `object` and `companion object` are separate kinds. All map to
# the graph's single Class label (see module docstring).
_TYPE_DECL_TYPES = ("class_declaration", "object_declaration", "companion_object")
_FUNCTION_TYPES = ("function_declaration",)


def _make_parser() -> Parser:
    return Parser(_KOTLIN_LANGUAGE)


# A member closing on its own line's `}` (`class A { fun a() = 1 }`): the
# grammar wants a terminator before the brace and, without one, turns the
# rest of the file into one error node. The patch keeps every byte offset.
_UNTERMINATED_CLOSE = re.compile(rb"(?<=[^\s{;]) \}")

# Where a top-level declaration can start at column 0: a declaration keyword,
# a modifier, or an annotation.
_DECLARATION_START = re.compile(
    rb"^(?:@\w|(?:class|interface|object|fun|val|var|typealias|enum|data|sealed|abstract|open|internal|private"
    rb"|public|protected|annotation|inline|value|const|suspend|operator|infix|tailrec|external|expect|actual"
    rb"|override|lateinit)\b)",
    re.MULTILINE,
)


def _error_cost(node: Node) -> tuple[int, int]:
    """Bytes under error nodes (an error node's children not counted again),
    then the zero-width tokens the parser had to invent (a missing node,
    which a hidden token such as a member terminator leaves as an error flag
    on its parent alone)."""
    if node.type == "ERROR":
        return node.end_byte - node.start_byte, 0
    if node.is_missing:
        return 0, 1
    if not node.has_error:
        return 0, 0
    if not any(child.has_error for child in node.children):
        return 0, 1
    costs = [_error_cost(child) for child in node.children]
    return sum(c[0] for c in costs), sum(c[1] for c in costs)


#: Recovery parses at most this many pieces of a file, and at most this many
#: times the file's bytes in all; what is left stays unrecovered. Both limits
#: depend on the text alone, so recovery is deterministic.
_MAX_PIECES = 5000
_MAX_PARSED_FACTOR = 4


def _parse_piece(parser: Parser, source: bytes, start: int, end: int, line_starts: list[int]):
    """The tree of `source[start:end]` alone, parsed over that byte range
    (`included_ranges`), so its nodes keep their offsets in `source` and the
    cost follows the piece, not the file."""

    def point(offset: int) -> tuple[int, int]:
        row = bisect.bisect_right(line_starts, offset) - 1
        return row, offset - line_starts[row]

    parser.included_ranges = [Range(point(start), point(end), start, end)]
    return parser.parse(source)


def _better_of_two(parser: Parser, source: bytes) -> tuple:
    """The tree of `source`, or of its `_UNTERMINATED_CLOSE` patch when that
    has fewer bytes under error nodes (then fewer invented ones), with the
    bytes it parsed."""
    tree = parser.parse(source)
    if tree.root_node.has_error:
        patched = _UNTERMINATED_CLOSE.sub(b";}", source)
        if patched != source:
            other = parser.parse(patched)
            if _error_cost(other.root_node) < _error_cost(tree.root_node):
                return other, patched
    return tree, source


def _parse_kotlin(parser: Parser, source: bytes) -> tuple[Node, list[Node], list, bool]:
    """Parse `source`, recovering what tree-sitter-kotlin 1.1.0 loses after
    a one-line member body (audit item 11). Returns the root, its top-level
    nodes to visit, the trees that own them (kept alive by the caller) and
    whether every error was recovered.

    1. A tree with errors is parsed again with `_UNTERMINATED_CLOSE`'s
       same-length patch (` }` to `;}`); the tree with fewer bytes under error
       nodes (then fewer missing ones) wins, the original on a tie (the blind patch can break a string
       template such as `"${ x }"`).
    2. Each top-level error node left is split at column-0 declaration
       starts, and each piece is parsed alone over its own byte range
       (`_parse_piece`), so its declarations keep their offsets; they are
       visited in place of the error node (each piece patched as in 1. when
       that helps). At most `_MAX_PIECES` pieces and `_MAX_PARSED_FACTOR`
       times the file's bytes are parsed this way.

    Every node's text is read from the original `source`: offsets never move.
    """
    tree, chosen = _better_of_two(parser, source)
    trees = [tree]
    root = tree.root_node
    top_level: list[Node] = []
    clean = True
    patched = _UNTERMINATED_CLOSE.sub(b";}", chosen)
    line_starts = [0] + [m.end() for m in re.finditer(rb"\n", chosen)]
    piece_parser = _make_parser()
    pieces = parsed = 0
    for node in root.named_children:
        if node.type != "ERROR":
            top_level.append(node)
            clean = clean and not node.has_error
            continue
        starts = [m.start() for m in _DECLARATION_START.finditer(chosen, node.start_byte, node.end_byte)]
        clean = clean and bool(starts) and starts[0] == node.start_byte
        for start, end in zip(starts, [*starts[1:], node.end_byte]):
            if pieces >= _MAX_PIECES or parsed + 2 * (end - start) > _MAX_PARSED_FACTOR * len(source):
                clean = False
                break
            pieces += 1
            parsed += end - start
            piece = _parse_piece(piece_parser, chosen, start, end, line_starts)
            if piece.root_node.has_error and patched != chosen:
                parsed += end - start
                other = _parse_piece(piece_parser, patched, start, end, line_starts)
                if _error_cost(other.root_node) < _error_cost(piece.root_node):
                    piece = other
            trees.append(piece)
            clean = clean and not piece.root_node.has_error
            top_level += [child for child in piece.root_node.named_children if child.type != "ERROR"]
    return root, top_level, trees, clean


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


def _package_name(root: Node, source: bytes) -> str | None:
    """The file's `package` declaration, e.g. 'com.motonav.app.ride'."""
    for child in root.named_children:
        if child.type == "package_header":
            text = _text(child, source).strip()
            if text.startswith("package "):
                return text[len("package ") :].strip()
            return text
    return None


def _extract_imports(root: Node, source: bytes) -> list[tuple[str, bool, str | None]]:
    """Extract `import` declarations, returning (dotted_path, is_wildcard,
    alias).

    Only `import` nodes carrying a `qualified_identifier` child are real
    imports (the grammar also emits a bare `import` keyword node). A wildcard
    import (`import a.b.*`) has its trailing `.*` stripped and `is_wildcard`
    set; `import a.b.f as g` has the alias `g`.
    """
    imports: list[tuple[str, bool, str | None]] = []
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
        alias = None
        if any(c.type == "as" for c in child.children):
            alias_node = child.named_children[-1]
            alias = _text(alias_node, source) if alias_node.type == "identifier" else None
        imports.append((dotted, is_wildcard, alias))
    return imports


# Kotlin and Java library types beyond STOP_TYPES: a call on one links
# nothing (they are never a repository file).
_LIBRARY_TYPES = frozenset({
    "Any", "Unit", "Nothing", "Int", "Char", "Byte", "Short", "Float", "Array", "IntArray", "ByteArray",
    "MutableList", "MutableMap", "MutableSet", "Collection", "Iterable", "Sequence", "Pair", "Triple",
    "Exception", "RuntimeException", "IllegalArgumentException", "IllegalStateException", "Throwable",
    "ArrayList", "HashMap", "HashSet", "LinkedHashMap", "File", "Path", "Duration", "Instant", "UUID",
})


def _kotlin_type(node: Node | None, source: bytes) -> str | None:
    """A declared type as the simple name the resolver looks up (`Repo?`
    -> Repo, `a.b.C<T>` -> C); None for a function type or no type."""
    if node is None:
        return None
    if node.type == "nullable_type":
        return _kotlin_type(next((c for c in node.named_children), None), source)
    if node.type == "user_type":
        names = [_text(c, source) for c in node.named_children if c.type == "identifier"]
        return names[-1] if names else None
    return None


def _declared_type(declaration: Node, source: bytes) -> str | None:
    """The type a `val x: T` / `val x = T()` / class parameter declares: its
    annotation, else a constructor call's class (a capitalised callee)."""
    variable = _child_of_type(declaration, "variable_declaration") or declaration
    for child in variable.named_children:
        if child.type in ("user_type", "nullable_type"):
            return _kotlin_type(child, source)
    for child in declaration.named_children:
        if child.type == "call_expression":
            callee = child.named_children[0] if child.named_children else None
            if callee is not None and callee.type == "identifier" and _text(callee, source)[:1].isupper():
                return _text(callee, source)
    return None


def _declared_name(declaration: Node, source: bytes) -> str | None:
    variable = _child_of_type(declaration, "variable_declaration") or declaration
    name = _child_of_type(variable, "identifier")
    return _text(name, source) if name is not None else None


class _KotlinResolver:
    """Resolves one Kotlin file's call sites to path pins: see the module
    docstring's tiers."""

    def __init__(self, root: Node, top_level: list[Node], source: bytes, file_path: str, package: str | None):
        self.source = source
        self.file_path = file_path
        self.layout = JvmLayout(file_path, package)
        # The file's declarations: every function name, and each class's
        # methods, bases and properties.
        self.functions: set[str] = set()
        self.methods: dict[str, set[str]] = {}
        self.bases: dict[str, list[str]] = {}
        self.properties: dict[str, dict[str, str | None]] = {}

        def walk(node: Node, owner: str | None) -> None:
            if node.type in _TYPE_DECL_TYPES:
                name_node = node.child_by_field_name("name") or _child_of_type(node, "identifier")
                if name_node is not None:
                    owner = _text(name_node, source)
                    self.methods.setdefault(owner, set())
                    self.bases[owner] = _extract_supertype_names(node, source)
                    props = self.properties.setdefault(owner, {})
                    ctor = _child_of_type(node, "primary_constructor")
                    params = _child_of_type(ctor, "class_parameters") if ctor is not None else None
                    for param in params.named_children if params is not None else []:
                        if param.type == "class_parameter" and any(c.type in ("val", "var") for c in param.children):
                            name = _child_of_type(param, "identifier")
                            if name is not None:
                                props[_text(name, source)] = _declared_type(param, source)
                    body = _child_of_type(node, "class_body") or _child_of_type(node, "enum_class_body")
                    for child in body.named_children if body is not None else []:
                        if child.type == "property_declaration":
                            name = _declared_name(child, source)
                            if name is not None:
                                props[name] = _declared_type(child, source)
            elif node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    self.functions.add(_text(name_node, source))
                    if owner is not None:
                        self.methods[owner].add(_text(name_node, source))
            for child in node.children:
                walk(child, owner)

        for node in top_level:
            walk(node, None)
        # Imports: a class (the first capitalised segment) or a function by
        # the name it is used by, and wildcard packages.
        self.types: dict[str, tuple[list[str], str]] = {}
        self.imported_functions: dict[str, list[str]] = {}
        self.wildcards: list[list[str]] = []
        self.symbol_imports: list[tuple[str, str, list[str]]] = []  # (label, name, pins)
        self.module_pins: set[str] = set()
        for dotted, is_wildcard, alias in _extract_imports(root, source):
            segments = dotted.split(".")
            at = type_segment(segments)
            if at is None:
                if is_wildcard:
                    self.wildcards.append(segments)
                    self.module_pins |= self._package_import_pins(segments)
                elif len(segments) > 1:
                    package, name = segments[:-1], segments[-1]
                    self.imported_functions[alias or name] = self.function_pins(package)
                    self.symbol_imports.append(("Function", name, self._symbol_pins(package)))
                continue
            package, cls = segments[:at], segments[at]
            self.symbol_imports.append(("Class", cls, self._symbol_pins(package)))
            if is_wildcard or at < len(segments) - 1:
                # Members of an object or companion (`a.b.Obj.f`, `a.b.Obj.*`).
                if not is_wildcard:
                    self.imported_functions[alias or segments[-1]] = self.class_pins(package, cls)
                continue
            self.types[alias or cls] = (package, cls)

    # --- pins ----------------------------------------------------------------

    def _suffixes(self, package: list[str]) -> tuple[str | None, str | None]:
        return self.layout.suffix(package), self.layout.suffix(package, full=True)

    def _package_import_pins(self, package: list[str]) -> set[str]:
        """A wildcard import's Module pins: the package's directory under this
        source root, and by suffix anywhere else (Kotlin and Java files)."""
        pins = set()
        folder = self.layout.folder(package)
        if folder is not None:
            pins.add((f"{folder}/." if folder else ".") + "?.kt,.java")
        rel, full = self._suffixes(package)
        if rel is not None:
            pins.add(f"{rel}/.?.kt{self.layout.flag}")
        if full is not None:
            pins.add(f"{full}/.?.java{self.layout.flag}")
        return pins

    def _symbol_pins(self, package: list[str]) -> list[str]:
        """Where an imported class or function is declared: a file of its
        package (a Kotlin file can hold several, named after none of them)."""
        return sorted(self._package_import_pins(package))

    def function_pins(self, package: list[str]) -> list[str]:
        """Where a top-level function of `package` is: a file of the
        package's directory under this source root, else of any directory
        ending in the package."""
        pins = []
        folder = self.layout.folder(package)
        if folder is not None:
            pins.append((f"{folder}/." if folder else ".") + "?.kt")
        rel, _full = self._suffixes(package)
        if rel is not None:
            pins.append(f"{rel}/.?.kt{self.layout.flag}")
        return pins

    def class_pins(self, package: list[str], cls: str) -> list[str]:
        """Where a member of the class `cls` of `package` is: its own `.kt`
        file, else any Kotlin file of its package (at package confidence),
        under this source root or another; or a Java file of the package."""
        pins = []
        folder = self.layout.folder(package)
        if folder is not None:
            pins.append(posixpath.join(folder, f"{cls}.kt") if folder else f"{cls}.kt")
            pins.append((f"{folder}/." if folder else ".") + "~?.kt")
        # Elsewhere a file of the package's directories, at package confidence
        # either way, so its own `.kt`/`.java` name adds nothing.
        rel, full = self._suffixes(package)
        if rel is not None:
            pins.append(f"{rel}/.?.kt{self.layout.flag}")
        if full is not None:
            pins.append(f"{full}/.?.java{self.layout.flag}")
        return pins

    def type_pins(self, name: str) -> list[str] | None:
        """Where a member of the class `name` (not one of this file's) is;
        None for a library type."""
        if name in STOP_TYPES["kotlin"] or name in _LIBRARY_TYPES:
            return None
        if name in self.types:
            return self.class_pins(*self.types[name])
        pins = self.class_pins(self.layout.package, name)
        for package in self.wildcards:
            pins += self.class_pins(package, name)
        return pins

    def member_pins(self, cls: str, method: str, seen: frozenset = frozenset()) -> list[str] | None:
        """Where `method` of the class `cls` can be: this file when the class
        is declared here and has it, else its bases', else the class's own
        pins for a class declared elsewhere."""
        if cls in seen:
            return None
        if cls in self.methods:
            if method in self.methods[cls]:
                return [self.file_path]
            pins = []
            for base in self.bases.get(cls, []):
                pins += self.member_pins(base, method, seen | {cls}) or []
            return pins or None
        return self.type_pins(cls)

    # --- calls ---------------------------------------------------------------

    def resolve(self, call: Node, classes: list[str], scope: dict[str, str | None]):
        """One `call_expression` as (name, pins or None for a bare-name row,
        member call?), or None when it links nothing."""
        callee = call.named_children[0] if call.named_children else None
        if callee is None:
            return None
        if callee.type == "identifier":
            name = _text(callee, self.source)
            if name in scope:
                return None  # a function value
            if name in self.functions:
                return name, [self.file_path], False
            if name in self.imported_functions:
                return name, self.imported_functions[name], False
            pins = []
            for cls in classes:
                for base in self.bases.get(cls, []):
                    pins += self.member_pins(base, name) or []
            pins += self.function_pins(self.layout.package)
            for package in self.wildcards:
                pins += self.function_pins(package)
            return (name, pins, False) if pins else None
        if callee.type != "navigation_expression":
            return None
        names = [c for c in callee.children if c.type == "identifier"]
        receiver = callee.named_children[0] if callee.named_children else None
        if not names or receiver is None or receiver is names[-1]:
            return None
        name = _text(names[-1], self.source)
        if receiver.type == "this_expression":
            pins = self.member_pins(classes[0], name) if classes else None
            return (name, pins, False) if pins else None
        if receiver.type == "super_expression":
            pins = [pin for base in (self.bases.get(classes[0], []) if classes else [])
                    for pin in self.member_pins(base, name) or []]
            return (name, pins, False) if pins else None
        if name in self.imported_functions:
            # An imported extension function (`note.isStale(now)`).
            return name, self.imported_functions[name], True
        typed, cls = self.receiver_type(receiver, scope)
        if typed:
            pins = self.member_pins(cls, name) if cls else None
            return (name, pins, True) if pins else None
        if name in STOP_METHODS["kotlin"]:
            return None
        return name, None, True

    def receiver_type(self, node: Node, scope: dict[str, str | None]) -> tuple[bool, str | None]:
        """(whether the receiver's type is known, the class name, None for
        one that links nothing)."""
        if node.type == "parenthesized_expression" and node.named_children:
            inner = node.named_children[0]
            if inner.type == "as_expression":
                return True, _kotlin_type(inner.child_by_field_name("right"), self.source)
            return self.receiver_type(inner, scope)
        if node.type == "identifier":
            text = _text(node, self.source)
            if text in scope:
                return scope[text] is not None, scope[text]
            if text[:1].isupper():
                return True, text  # an object or a companion: a static call
            return False, None
        if node.type == "call_expression":
            callee = node.named_children[0] if node.named_children else None
            if callee is not None and callee.type == "identifier":
                text = _text(callee, self.source)
                if text[:1].isupper() and text not in self.functions:
                    return True, text  # a constructor call
        if node.type == "string_literal":
            return True, None
        return False, None


def _kotlin_scope(node: Node, classes: list[str], resolver: _KotlinResolver, source: bytes) -> dict[str, str | None]:
    """The names typed in reach of a function body: the enclosing classes'
    properties (innermost wins), the parameters and every local (a lambda
    or loop variable untyped unless annotated)."""
    scope: dict[str, str | None] = {}
    for cls in reversed(classes):
        scope.update(resolver.properties.get(cls, {}))

    def walk(n: Node) -> None:
        if n.type == "parameter":
            name = _child_of_type(n, "identifier")
            if name is not None:
                scope[_text(name, source)] = _declared_type(n, source)
        elif n.type == "property_declaration":
            name = _declared_name(n, source)
            if name is not None:
                scope[name] = _declared_type(n, source)
        elif n.type in ("for_statement", "lambda_parameters"):
            for variable in n.named_children:
                if variable.type == "variable_declaration":
                    name = _child_of_type(variable, "identifier")
                    if name is not None:
                        scope[_text(name, source)] = _declared_type(variable, source)
        elif n.type == "lambda_literal":
            scope.setdefault("it", None)
        for child in n.children:
            if child.type not in _TYPE_DECL_TYPES:
                walk(child)

    walk(node)
    return scope


def _kotlin_calls(body: Node) -> list[Node]:
    """The `call_expression`s of a body, not descending into nested
    function, class or object declarations (attributed separately)."""
    found: list[Node] = []

    def walk(node: Node) -> None:
        if node.type in (*_TYPE_DECL_TYPES, *_FUNCTION_TYPES):
            return
        if node.type == "call_expression":
            found.append(node)
        for child in node.children:
            walk(child)

    for child in body.children:
        walk(child)
    return found


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
    root, top_level, _trees, clean = _parse_kotlin(parser, source_bytes)

    if not clean:
        logger.warning(f"Syntax errors while parsing {file_path}; extracting best-effort result")

    module_node = GraphNode(
        label="Module",
        repo_id=repo_id,
        name=file_path,
        properties={"type": "module", "source_file": file_path},
    )
    result.nodes.append(module_node)

    resolver = _KotlinResolver(root, top_level, source_bytes, file_path, _package_name(root, source_bytes))
    result.relationships += import_rows(file_path, repo_id, set(), resolver.module_pins)
    # An imported class or function: the file of its package declaring it.
    symbols: dict[tuple[str, str], set[str]] = {}
    for label, name, pins in resolver.symbol_imports:
        symbols.setdefault((label, name), set()).update(pins)
    for (label, name), pins in sorted(symbols.items()):
        result.relationships += call_rows(
            "Module", file_path, name, pins, False, None, file_path, repo_id, rel_type="IMPORTS", to_label=label,
        )
    sites = CallSites()

    def _record_calls(node: Node, body: Node, caller_label: str, caller_name: str, classes: list[str]) -> None:
        scope = _kotlin_scope(node, classes, resolver, source_bytes)
        caller_class = classes[0] if classes else None
        for call in _kotlin_calls(body):
            site = resolver.resolve(call, classes, scope)
            if site is not None:
                name, pins, member = site
                sites.add(caller_label, caller_name, caller_class, name, pins, member)

    def _visit_type_decl(
        node: Node, parent_name: str | None, parent_label: str, prev_sibling: Node | None, classes: list[str]
    ) -> None:
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
            visit_block(body_node, class_name, "Class", [class_name, *classes])

    def _visit_function(
        node: Node, parent_name: str | None, parent_label: str, prev_sibling: Node | None, classes: list[str]
    ) -> None:
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
            _record_calls(node, body_node, "Function", func_name, classes)

            # Nested function declarations (a `fun` declared inside this
            # function's body) are visited with this function as their parent,
            # so their own calls are attributed to them rather than hoisted
            # here — `_kotlin_calls` stops descending at nested
            # function/class scopes, mirroring Python's nested-function walk.
            for child in body_node.named_children:
                if child.type == "function_declaration":
                    _visit_function(child, func_name, "Function", None, classes)
                elif child.type == "block":
                    for grandchild in child.named_children:
                        if grandchild.type == "function_declaration":
                            _visit_function(grandchild, func_name, "Function", None, classes)

    def visit_block(
        block: Node, parent_name: str | None, parent_label: str, classes: list[str],
        children: list[Node] | None = None,
    ) -> None:
        """Visit declarations in a block (module/class/object body), or the
        given `children` of it (the file's recovered top level); `classes`
        are the enclosing classes, innermost first."""
        prev_sibling: Node | None = None
        for node in block.named_children if children is None else children:
            if node.type in _TYPE_DECL_TYPES:
                _visit_type_decl(node, parent_name, parent_label, prev_sibling, classes)
            elif node.type in _FUNCTION_TYPES:
                _visit_function(node, parent_name, parent_label, prev_sibling, classes)
            elif node.type == "block_comment":
                pass  # only relevant as a *following* node's kdoc
            else:
                # Property/initializer/statement: not a def, but may contain
                # calls — attribute those to the enclosing scope.
                _record_calls(node, node, parent_label, parent_name if parent_name else file_path, classes)
            prev_sibling = node

    visit_block(root, None, "Module", [], top_level)
    result.relationships += sites.rows(file_path, repo_id)

    return own_edges(result, file_path)


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
