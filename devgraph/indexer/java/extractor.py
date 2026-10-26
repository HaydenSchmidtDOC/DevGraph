"""Tree-sitter-based Java source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape and philosophy —
same GraphNode/GraphRelationship/ExtractionResult dataclasses (shared via
`devgraph.indexer.common`), CALLS and IMPORTS resolved to path pins
(`_JavaResolver`; see the Java and Kotlin section of
docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md).
Extracts:

  - Module (the file itself)
  - Class/interface/enum declarations, with `extends`/`implements` -> EXTENDS
  - Methods and constructors (Java has no free functions — every method
    belongs to a class/interface/enum, same situation as C#), with CONTAINS
  - Javadoc (`/** ... */`) immediately preceding a class/method/constructor,
    stored the same way Python's docstring is (`description`/`docstring_full`)
  - `import` statements, resolved via Java's package/folder convention
    (`jvm.JvmLayout`: the source root a file's package gives) to the class
    file under the same root and, by path suffix, under any other (another
    module); a wildcard to the package's directory.
  - CALLS, by tier: the enclosing classes and their bases, static imports,
    then typed receivers (fields, parameters, locals, `new T()`, `T.m()`)
    to their class's file; an untyped receiver by bare name unless its
    method is stoplisted.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from devgraph.indexer.apis.routes import (
    SPRING_CONTROLLERS,
    add_route,
    spring_implementer_key,
    spring_interface_pins,
    spring_routes,
    spring_type_names,
)
from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, CallSites, import_rows
from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    add_heir_keys,
    class_member_pin,
    own_edges,
    owned_pin,
)
from devgraph.indexer.jvm import JvmLayout, type_segment

logger = logging.getLogger(__name__)

_JAVA_LANGUAGE = Language(tsjava.language())

# class_declaration, interface_declaration, enum_declaration and
# record_declaration are all treated as "Class" nodes (Java has no separate
# interface/enum/record label in the graph schema, matching how the generic
# Class/Function/Module labels are meant to generalize across languages). A
# record's compact canonical constructor (`P { ... }`) is a constructor.
_TYPE_DECL_TYPES = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration")
_METHOD_DECL_TYPES = ("method_declaration", "constructor_declaration", "compact_constructor_declaration")


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


def _package_name(root: Node, source: bytes) -> str | None:
    for child in root.named_children:
        if child.type == "package_declaration":
            # package_declaration := 'package' (scoped_identifier | identifier) ';'
            for c in child.named_children:
                if c.type in ("scoped_identifier", "identifier"):
                    return _text(c, source)
    return None


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


# java.lang and other library types beyond STOP_TYPES: a call on one links
# nothing (they are never a repository file).
_LIBRARY_TYPES = frozenset({
    "Object", "Exception", "RuntimeException", "IllegalArgumentException", "IllegalStateException", "Throwable",
    "Error", "Iterable", "Iterator", "Comparable", "Runnable", "Class", "Void", "Float", "Short", "Byte",
    "ArrayList", "LinkedList", "HashMap", "HashSet", "TreeMap", "TreeSet", "LinkedHashMap", "LinkedHashSet",
    "Deque", "ArrayDeque", "Queue", "Collection", "BigDecimal", "BigInteger", "LocalDate", "LocalDateTime",
    "Instant", "Duration", "UUID", "Pattern", "Matcher", "Files", "Paths", "Path", "File",
})

_FUNC_TYPES = (*_METHOD_DECL_TYPES, "lambda_expression")


def _members(body: Node) -> list[Node]:
    """A type body's member declarations: an enum's methods, fields and
    nested types sit in its `enum_body_declarations`, and an enum
    constant's own body (`ACTIVE { void x() {} }`) declares members of the
    enum too (one node per name and owner, as overloads are)."""
    found: list[Node] = []
    for child in body.named_children:
        if child.type == "enum_body_declarations":
            found += child.named_children
        elif child.type == "enum_constant":
            constant_body = child.child_by_field_name("body")
            found += constant_body.named_children if constant_body is not None else []
        else:
            found.append(child)
    return found


def _type_name(node: Node | None, source: bytes) -> str | None:
    """A declared type as the simple name the resolver looks up (`List<X>`
    -> List, `a.b.C` -> C, `Outer.Inner` -> Inner); None for a primitive,
    an array, `var` or no type."""
    if node is None:
        return None
    if node.type == "generic_type":
        return _type_name(node.named_children[0] if node.named_children else None, source)
    if node.type == "type_identifier":
        name = _text(node, source)
        return None if name == "var" else name
    if node.type == "scoped_type_identifier":
        return _text(node, source).rsplit(".", 1)[-1]
    return None


class _JavaResolver:
    """Resolves one Java file's call sites to path pins: see the module
    docstring's tiers."""

    def __init__(self, root: Node, source: bytes, file_path: str, package: str | None) -> None:
        self.source = source
        self.file_path = file_path
        self.layout = JvmLayout(file_path, package)
        # The file's types: their methods, bases and fields.
        self.methods: dict[str, set[str]] = {}
        self.bases: dict[str, list[str]] = {}
        self.fields: dict[str, dict[str, str | None]] = {}
        # What each type's members are owned by (_owner_of): one per type of the name in the file.
        self.owners: dict[str, set[str]] = {}

        def walk(node: Node, enclosing: tuple[str, ...]) -> None:
            if node.type in _TYPE_DECL_TYPES:
                name_node = node.child_by_field_name("name")
                body = node.child_by_field_name("body")
                if name_node is not None:
                    name = _text(name_node, source)
                    enclosing = (name, *enclosing)
                    self.owners.setdefault(name, set()).add(_owner_of(enclosing))
                    self.methods.setdefault(name, set())
                    self.bases[name] = _extract_supertype_names(node, source)
                    fields = self.fields.setdefault(name, {})
                    for child in _members(body) if body is not None else []:
                        if child.type in _METHOD_DECL_TYPES:
                            method = child.child_by_field_name("name")
                            if method is not None:
                                self.methods[name].add(_text(method, source))
                        elif child.type == "field_declaration":
                            declared = _type_name(child.child_by_field_name("type"), source)
                            for decl in child.children_by_field_name("declarator"):
                                field_name = decl.child_by_field_name("name")
                                if field_name is not None:
                                    fields[_text(field_name, source)] = declared
            for child in node.children:
                walk(child, enclosing)

        walk(root, ())
        # Imports: a simple name -> its class's package and class (the
        # first capitalised segment picks the file); static members the same;
        # wildcard packages, and the classes of static wildcards.
        self.types: dict[str, tuple[list[str], str]] = {}
        self.type_owners: dict[str, str] = {}
        self.statics: dict[str, tuple[list[str], str]] = {}
        self.static_wildcards: list[tuple[list[str], str]] = []
        self.wildcards: list[list[str]] = []
        self.class_wildcards: list[list[str]] = []  # `import a.b.Outer.*;`: the classes nested in Outer
        self.import_files: set[str] = set()
        self.import_pins: set[str] = set()
        for dotted, is_static, is_wildcard in _extract_imports(root, source):
            segments = dotted.split(".")
            at = type_segment(segments)
            if at is None:
                if is_wildcard and not is_static:
                    self.wildcards.append(segments)
                    self._import_package(segments)
                elif len(segments) > 1:
                    # A lowercase class name: the last segment (less a static member).
                    cls_at = len(segments) - (2 if is_static else 1)
                    if cls_at > 0:
                        self._import_class(segments[:cls_at], segments[cls_at])
                continue
            owner = (segments[:at], segments[at])
            self._import_class(*owner)
            if is_static:
                if is_wildcard:
                    self.static_wildcards.append(owner)
                elif at < len(segments) - 1:
                    self.statics[segments[-1]] = owner
            elif is_wildcard:
                self.class_wildcards.append(segments)
            else:
                self.types[segments[-1]] = owner
                # Its members' owner: a nested class's is dotted (`Settings.Defaults`).
                self.type_owners[segments[-1]] = ".".join(segments[at:])

    # --- imports -------------------------------------------------------------

    def _import_class(self, package: list[str], cls: str) -> None:
        folder = self.layout.folder(package)
        if folder is not None:
            self.import_files.add(posixpath.join(folder, f"{cls}.java") if folder else f"{cls}.java")
        suffix = self.layout.suffix(package, full=True)
        if suffix is not None:
            self.import_pins.add(f"{suffix}/{cls}.java{self.layout.flag}")

    def _import_package(self, package: list[str]) -> None:
        folder = self.layout.folder(package)
        if folder is not None:
            self.import_pins.add((f"{folder}/." if folder else ".") + "?.java")
        suffix = self.layout.suffix(package, full=True)
        if suffix is not None:
            self.import_pins.add(f"{suffix}/.?.java{self.layout.flag}")

    # --- pins ----------------------------------------------------------------

    def _class_file_pins(self, package: list[str], cls: str) -> list[str]:
        pins = []
        folder = self.layout.folder(package)
        if folder is not None:
            pins.append(posixpath.join(folder, f"{cls}.java") if folder else f"{cls}.java")
        suffix = self.layout.suffix(package, full=True)
        if suffix is not None:
            pins.append(f"{suffix}/{cls}.java{self.layout.flag}")
        return pins

    def type_pins(self, name: str) -> list[str] | None:
        """Where the class `name` (not one of this file's) is: its import's
        file, else its own package's or a wildcard package's; None for a
        library type."""
        if name in STOP_TYPES["java"] or name in _LIBRARY_TYPES:
            return None
        if name in self.types:
            return self._class_file_pins(*self.types[name])
        pins = self._class_file_pins(self.layout.package, name)
        for package in self.wildcards:
            pins += self._class_file_pins(package, name)
        return pins

    def member_pins(self, cls: str, method: str, seen: frozenset = frozenset()) -> list[str] | None:
        """Where `method` of the class `cls` can be: this file when the class
        is declared here and has it, else its bases', else the class's own
        file for a class declared elsewhere. A method of this file names its
        class as its owner (common.owned_pin), unless two classes of the file
        share the name; another file's class's member names that class."""
        if cls in seen:
            return None
        if cls in self.methods:
            if method in self.methods[cls]:
                owners = self.owners.get(cls, set())
                return [owned_pin(self.file_path, next(iter(owners)) if len(owners) == 1 else None)]
            pins = []
            for base in self.bases.get(cls, []):
                pins += self.member_pins(base, method, seen | {cls}) or []
            return pins or None
        # Another file's class: its member there, its own or one it inherits
        # from a class of its file (common.heir_keys).
        pins = self.type_pins(cls)
        owner = self.type_owners.get(cls, cls)
        return [class_member_pin(pin, owner) for pin in pins] if pins is not None else None

    # --- calls ---------------------------------------------------------------

    def resolve(self, call: Node, classes: list[str], scope: dict[str, str | None]):
        """One `method_invocation` as (name, pins or None for a bare-name
        row, member call?), or None when it links nothing. `classes` are the
        enclosing classes, innermost first; `scope` the typed names in
        reach."""
        name_node = call.child_by_field_name("name")
        if name_node is None:
            return None
        name = _text(name_node, self.source)
        obj = call.child_by_field_name("object")
        if obj is None:
            for cls in classes:
                pins = self.member_pins(cls, name)
                if pins:
                    return name, pins, False
            if name in self.statics:
                return name, self._class_file_pins(*self.statics[name]), False
            pins = [pin for owner in self.static_wildcards for pin in self._class_file_pins(*owner)]
            return (name, pins, False) if pins else None
        if obj.type == "this":
            pins = self.member_pins(classes[0], name) if classes else None
            return (name, pins, False) if pins else None
        if obj.type == "super":
            pins = [
                pin for base in (self.bases.get(classes[0], []) if classes else [])
                for pin in self.member_pins(base, name) or []
            ]
            return (name, pins, False) if pins else None
        typed, cls = self.receiver_type(obj, classes, scope)
        if typed:
            pins = self.member_pins(cls, name) if cls else None
            return (name, pins, True) if pins else None
        if name in STOP_METHODS["java"]:
            return None
        return name, None, True

    def receiver_type(self, obj: Node, classes: list[str], scope: dict[str, str | None]) -> tuple[bool, str | None]:
        """(whether the receiver's type is known, the class name, None for
        one that links nothing)."""
        if obj.type == "parenthesized_expression" and obj.named_children:
            return self.receiver_type(obj.named_children[0], classes, scope)
        if obj.type == "identifier":
            text = _text(obj, self.source)
            if text in scope:
                return scope[text] is not None, scope[text]
            if text[:1].isupper():
                return True, text  # a class: a static call
            return False, None
        if obj.type == "object_creation_expression":
            return True, _type_name(obj.child_by_field_name("type"), self.source)
        if obj.type == "field_access":
            owner = obj.child_by_field_name("object")
            field = obj.child_by_field_name("field")
            if owner is not None and field is not None:
                if owner.type == "this":
                    for cls in classes:
                        declared = self.fields.get(cls, {})
                        if _text(field, self.source) in declared:
                            return declared[_text(field, self.source)] is not None, declared[_text(field, self.source)]
                elif owner.type == "identifier" and _text(owner, self.source) in STOP_TYPES["java"]:
                    return True, None  # System.out
        return False, None


def _method_scope(node: Node, classes: list[str], resolver: _JavaResolver, source: bytes) -> dict[str, str | None]:
    """The names typed in reach of a method body: the enclosing classes'
    fields (innermost wins), the parameters, and every local, catch,
    resource, for-each and pattern variable of the body (by declared type, or
    by `new T()` for `var`); a lambda parameter is untyped."""
    scope: dict[str, str | None] = {}
    for cls in reversed(classes):
        scope.update(resolver.fields.get(cls, {}))

    def walk(n: Node) -> None:
        if n.type in ("formal_parameter", "catch_formal_parameter", "enhanced_for_statement", "resource"):
            name = n.child_by_field_name("name")
            declared = _type_name(n.child_by_field_name("type"), source)
            if declared is None and n.type == "resource":
                value = n.child_by_field_name("value")
                if value is not None and value.type == "object_creation_expression":
                    declared = _type_name(value.child_by_field_name("type"), source)
            if name is not None:
                scope[_text(name, source)] = declared
        elif n.type == "spread_parameter":
            for decl in n.named_children:
                if decl.type == "variable_declarator" and decl.child_by_field_name("name") is not None:
                    scope[_text(decl.child_by_field_name("name"), source)] = None
        elif n.type == "local_variable_declaration":
            declared = _type_name(n.child_by_field_name("type"), source)
            for decl in n.children_by_field_name("declarator"):
                name = decl.child_by_field_name("name")
                value = decl.child_by_field_name("value")
                typed = declared
                if typed is None and value is not None and value.type == "object_creation_expression":
                    typed = _type_name(value.child_by_field_name("type"), source)
                if name is not None:
                    scope[_text(name, source)] = typed
        elif n.type == "lambda_expression":
            params = n.child_by_field_name("parameters")
            if params is not None and params.type == "identifier":
                scope[_text(params, source)] = None
            for p in params.named_children if params is not None else []:
                if p.type == "identifier":
                    scope[_text(p, source)] = None
        for child in n.children:
            if child.type not in _TYPE_DECL_TYPES:
                walk(child)

    walk(node)
    return scope


def _owner_of(classes: list[str] | tuple[str, ...]) -> str:
    """The owner (common.member_owner) of a member of the innermost of
    `classes`, the enclosing classes innermost first: their names, outermost
    first, dotted ("" for a top-level type)."""
    return ".".join(reversed(classes))


def _invocations(body: Node) -> list[Node]:
    """The `method_invocation`s of a body, not descending into nested
    classes (attributed separately)."""
    found: list[Node] = []

    def walk(node: Node) -> None:
        if node.type in (*_TYPE_DECL_TYPES, *_METHOD_DECL_TYPES):
            return
        if node.type == "method_invocation":
            found.append(node)
        for child in node.children:
            walk(child)

    for child in body.children:
        walk(child)
    return found


def _annotations(node: Node, source: bytes) -> dict[str, dict[str, list[str] | None]]:
    """A declaration's annotations by name, each as its `value` (the
    positional argument, or `value =`/`path =`: its string literals, None
    when one is not a literal) and `method` (the RequestMethod names) when
    given (apis/routes.py `spring_routes`)."""
    found: dict[str, dict[str, list[str] | None]] = {}
    modifiers = _child_of_type(node, "modifiers")
    for annotation in modifiers.named_children if modifiers is not None else []:
        name = annotation.child_by_field_name("name")
        if annotation.type not in ("marker_annotation", "annotation") or name is None:
            continue
        args: dict[str, list[str] | None] = {}
        arguments = annotation.child_by_field_name("arguments")
        for arg in arguments.named_children if arguments is not None else []:
            key, value = "value", arg
            if arg.type == "element_value_pair":
                key_node, value = arg.child_by_field_name("key"), arg.child_by_field_name("value")
                key = _text(key_node, source) if key_node is not None else ""
            if value is None:
                continue
            items = value.named_children if value.type == "element_value_array_initializer" else [value]
            if key == "method":
                args["method"] = [_text(item, source).rsplit(".", 1)[-1] for item in items]
            elif key in ("value", "path"):
                literal = all(item.type == "string_literal" and not _text(item, source).startswith('"""')
                              for item in items)
                args["value"] = [_text(item, source)[1:-1] for item in items] if literal else None
        found[_text(name, source).rsplit(".", 1)[-1]] = args
    return found


def _written_type(node: Node, source: bytes) -> str:
    """A type as written, its type arguments left off (`a.B<C>` -> a.B)."""
    if node.type == "generic_type" and node.named_children:
        node = node.named_children[0]
    return "".join(_text(node, source).split())


def _spring_routes(
    root: Node, source: bytes, result: ExtractionResult, file_path: str, repo_id: str, resolver: _JavaResolver,
) -> dict[tuple[str, str], set[str]]:
    """A Spring controller's mapped methods as Endpoints (apis/routes.py),
    each implemented by its method, owned by its class; an interface's
    mapped methods too, implemented by its method and each implementing
    controller's (`spring_interface_pins`). Returns the match keys
    (`spring_implementer_key`) of each controller method (name, owner) for
    the interfaces its class implements, by their qualified names
    (`spring_type_names`, through the file's package and imports). A
    controller with a `@RequestMapping` of its own gets none: Spring maps
    its interface methods under its own prefix, which no interface's route
    names (see the README's current limitations)."""
    keys: dict[tuple[str, str], set[str]] = {}
    package = resolver.layout.package
    imported = {name: ".".join([*owner[0], resolver.type_owners[name]]) for name, owner in resolver.types.items()}

    def walk(block: Node, classes: list[str]) -> None:
        for node in block.named_children:
            name = node.child_by_field_name("name")
            body = node.child_by_field_name("body")
            if node.type not in _TYPE_DECL_TYPES or name is None or body is None:
                continue
            inner = [_text(name, source), *classes]
            mapped = _annotations(node, source)
            interface = node.type == "interface_declaration"
            implemented = _child_of_type(node, "super_interfaces")
            implemented = implemented.named_children[0].named_children \
                if implemented is not None and implemented.named_children else []
            for member in _members(body):
                method = member.child_by_field_name("name")
                if member.type == "method_declaration" and method is not None:
                    pins = spring_interface_pins(
                        file_path, _owner_of(inner), ".".join([*package, _owner_of(inner)])
                    ) if interface else None
                    for http_method, path, mounted in spring_routes(mapped, _annotations(member, source), interface):
                        add_route(result, repo_id, file_path, "Spring", http_method, path, _text(method, source),
                                  _owner_of(inner), pins, mounted)
                    if SPRING_CONTROLLERS & set(mapped) and "RequestMapping" not in mapped and implemented:
                        keys.setdefault((_text(method, source), _owner_of(inner)), set()).update(
                            spring_implementer_key(name) for each in implemented
                            for name in spring_type_names(_written_type(each, source), package, imported,
                                                          [*resolver.wildcards, *resolver.class_wildcards])
                        )
            walk(body, inner)

    walk(root, [])
    return keys


def _merge_constant_members(result: ExtractionResult, constant_members: set[int]) -> None:
    """An enum constant's body declares members of the enum (`ACTIVE {
    boolean isActive() {...} }`), one identity with the enum's own member
    of the name: such a node is dropped when the enum declares a member of
    its identity itself, or an earlier constant does, so the first
    declaration wins (as common.merge_same_nodes: its properties and
    lines), the enum's own before any constant's. Its calls stay the
    node's. Overloads the enum (or any class) declares itself stay
    separate nodes, as everywhere in Java."""

    def identity(node: GraphNode) -> tuple:
        return node.label, node.name, node.properties.get("file"), node.properties.get("owner") or ""

    seen = {identity(n) for n in result.nodes if id(n) not in constant_members and n.properties.get("file")}
    nodes: list[GraphNode] = []
    for node in result.nodes:
        if id(node) in constant_members:
            if identity(node) in seen:
                continue
            seen.add(identity(node))
        nodes.append(node)
    result.nodes = nodes


def extract_java_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a Java file and extract nodes and relationships.

    Args:
        source_code: The Java source as a string.
        file_path: The Module node's identity — the file's path relative
            to the repo root, forward-slashed (e.g.
            'src/main/java/com/example/foo/Bar.java'). Also gives the
            source root imports resolve under (see `jvm.JvmLayout`).
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

    resolver = _JavaResolver(root, source_bytes, file_path, _package_name(root, source_bytes))
    result.relationships += import_rows(file_path, repo_id, resolver.import_files, resolver.import_pins)
    sites = CallSites()
    # The nodes of enum constants' bodies (`ACTIVE { ... }`), merged into
    # the enum's member of their identity (`_merge_constant_members`).
    constant_members: set[int] = set()

    def _record_calls(
        node: Node, body: Node, caller_label: str, caller_name: str, classes: list[str]
    ) -> None:
        scope = _method_scope(node, classes, resolver, source_bytes)
        caller_class = classes[0] if classes else None
        # A method is owned by its classes; a class (a field initializer's
        # caller) by the classes around it; the Module by nothing.
        caller_owner = {"Function": _owner_of(classes), "Class": _owner_of(classes[1:])}.get(caller_label)
        for call in _invocations(body):
            site = resolver.resolve(call, classes, scope)
            if site is not None:
                name, pins, member = site
                sites.add(caller_label, caller_name, caller_class, name, pins, member, caller_owner)

    def visit_block(block: Node, parent_name: str | None, parent_label: str, classes: list[str]) -> None:
        """Visit statements in a block (module/class/interface/enum body);
        `classes` are the enclosing classes, innermost first."""
        prev_sibling: Node | None = None
        for node in block.named_children:
            if node.type in _TYPE_DECL_TYPES:
                _visit_class(node, parent_name, parent_label, prev_sibling, classes)
            elif node.type in _METHOD_DECL_TYPES:
                _visit_function(node, parent_name, parent_label, prev_sibling, classes)
            elif node.type == "block_comment":
                pass  # only relevant as a *following* node's javadoc, handled via prev_sibling
            elif node.type == "enum_body_declarations":
                visit_block(node, parent_name, parent_label, classes)  # an enum's methods, fields, nested types
            elif node.type == "enum_constant" and node.child_by_field_name("body") is not None:
                # A constant with a body: its arguments' calls are the
                # enum's, its body's members the enum's members.
                arguments = node.child_by_field_name("arguments")
                if arguments is not None:
                    _record_calls(node, arguments, parent_label, parent_name if parent_name else file_path, classes)
                first = len(result.nodes)
                visit_block(node.child_by_field_name("body"), parent_name, parent_label, classes)
                constant_members.update(id(each) for each in result.nodes[first:])
            else:
                # Field declarations, static/instance initializers, enum
                # constants, etc: not a def, but may still contain calls
                # (e.g. a field initializer) — attribute those to the
                # enclosing scope, mirroring Python's else-branch handling
                # of module/class-body-level statements.
                _record_calls(node, node, parent_label, parent_name if parent_name else file_path, classes)
            prev_sibling = node

    def _visit_class(
        node: Node, parent_name: str | None, parent_label: str, prev_sibling: Node | None, classes: list[str]
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        class_properties: dict = {
            "type": "class",
            "decorators": _extract_annotation_names(node, source_bytes),
            "file": file_path,
            "owner": _owner_of(classes),
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
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
                from_owner=_owner_of(classes[1:]) if parent_label == "Class" else None,
                to_owner=_owner_of(classes),
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
                    from_owner=_owner_of(classes),
                )
            )

        body_node = node.child_by_field_name("body")
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
            "owner": _owner_of(classes),
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
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
                from_owner=_owner_of(classes[1:]) if parent_label == "Class" else None,
                to_owner=_owner_of(classes),
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            _record_calls(node, body_node, "Function", func_name, classes)

    visit_block(root, None, "Module", [])
    _merge_constant_members(result, constant_members)
    result.relationships += sites.rows(file_path, repo_id)
    implementer_keys = _spring_routes(root, source_bytes, result, file_path, repo_id, resolver)
    add_heir_keys(result, [
        (name, owner, resolver.bases.get(name, []), methods)
        for name, methods in resolver.methods.items() for owner in resolver.owners.get(name, ())
    ])
    for node in result.nodes:
        if node.label == "Function" and (keys := implementer_keys.get((node.name, node.properties.get("owner")))):
            node.properties["mkeys"] = sorted({*node.properties.get("mkeys", ()), *keys})

    return own_edges(result, file_path)


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
