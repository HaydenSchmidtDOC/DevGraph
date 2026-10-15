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
  - IMPORTS: an import path resolves by the longest in-repository module
    path (every go.mod's `module`, and the local `replace`s of go.mod and
    go.work; resolver_config.ResolverConfig.go_package_dir) to its package's
    directory, written as a directory pin to that directory's non-test Go
    files (`dir/.?.go!_test.go`). Any other import is external: no edge.
  - CALLS, by the first tier that applies (see
    docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md):
    `f()` declared in this file -> this file; any other `f()` -> the
    file's package directory (a builtin links nothing); `pkg.F()` -> the
    imported package's directory, nothing for an external one (an
    unaliased in-repo import whose path name the file never uses is the
    package an unknown qualifier names, since its package clause can differ
    from its directory); `x.m()` on a typed `x` (receiver, parameter, `var
    x T`, `x := T{}`/`&T{}`/`new(T)`, a struct field of this file, what an
    in-file function returns, or what a package's function returned) ->
    this file when the type and `m` are declared here, else the type's
    package directory at package confidence (`~`); an untyped receiver ->
    a bare-name edge that never ends at the caller, nothing for a
    stoplisted method (calls.STOP_METHODS["go"]). Pins to the package's
    own files leave tests out from a non-test file; an external test
    package (`package x_test`) reaches only its directory's test files.
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
import posixpath
import re
from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Node, Parser

from devgraph.indexer.apis.routes import add_route, join_route
from devgraph.indexer.calls import STOP_METHODS, CallSites, import_rows
from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    own_edges,
    owned_pin,
)
from devgraph.indexer.resolver_config import ResolverConfig

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


def _receiver_type_name(method_node: Node, source: bytes) -> str | None:
    """Extract the struct name a method's receiver is attached to.

    `func (d *Dog) Speak()` and `func (d Dog) Speak()` both resolve to
    'Dog' — pointer vs. value receiver makes no difference to which struct
    the method is attributed to, and a generic receiver's type parameters
    are dropped: `func (s *Stack[T]) Push()` is 'Stack'.
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
            type_node = type_node.named_children[0]  # `*Stack[T]`: read `Stack[T]` as a value receiver
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


def _extract_imports(root: Node, source: bytes) -> list[tuple[str | None, str]]:
    """Each import spec as (its name, if given: an alias, `.` or `_`; the
    import path), e.g. (None, 'fmt'), ('db', 'example.com/app/legacydb')."""
    imports: list[tuple[str | None, str]] = []

    def walk(node: Node) -> None:
        if node.type == "import_spec":
            path_node = node.child_by_field_name("path")
            name_node = node.child_by_field_name("name")
            if path_node is not None:
                # interpreted_string_literal includes the surrounding quotes.
                path = _text(path_node, source).strip('"`')
                imports.append((_text(name_node, source) if name_node is not None else None, path))
        for child in node.children:
            walk(child)

    walk(root)
    return imports


_MAJOR_VERSION = re.compile(r"v[0-9]+")


def _default_import_name(import_path: str, module: str | None = None) -> str:
    """The name an unaliased import is used by, as far as its path tells:
    the last path element, skipping a major-version element and a
    gopkg.in-style `.vN` (`yaml.v3` is `yaml`). For an in-repository import
    (`module` the module path it resolved by) the last element is a major
    version only when it is the module's own (`example.com/shop/v2` is
    `shop`; `example.com/shop/v2/apis/apps/v1` is a directory, `package
    v1`); for an external one (`module` None) any `vN` is (`chi/v5` is
    `chi`). A package clause may name it otherwise (`legacydb/` declaring
    `package db`); see `_GoResolver.qualifier_dirs`."""
    parts = [part for part in import_path.split("/") if part]
    if len(parts) > 1 and _MAJOR_VERSION.fullmatch(parts[-1]) and (module is None or import_path == module):
        parts.pop()
    name = parts[-1] if parts else import_path
    name = re.sub(r"\.v[0-9]+$", "", name)
    if name.startswith("go-"):
        name = name[3:]
    return name.removesuffix("-go").replace("-", "_").replace(".", "_")


# Go's predeclared types and builtin functions: no file of the repository
# declares one.
_BUILTIN_TYPES = frozenset({
    "any", "bool", "byte", "comparable", "complex64", "complex128", "error", "float32", "float64", "int", "int8",
    "int16", "int32", "int64", "rune", "string", "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
})
_BUILTIN_FUNCS = frozenset({
    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag", "len", "make", "max", "min", "new",
    "panic", "print", "println", "real", "recover",
})


def _dir_pin(folder: str, only: str, not_suffix: str = "", loose: bool = False) -> str:
    """A directory pin (graph/schema.py `split_pin`) to the Go files of
    `folder` ("" the root): `only` the suffix they end with, less
    `not_suffix`; `loose` at package confidence."""
    pin = f"{folder}/." if folder else "."
    return pin + ("~" if loose else "") + f"?{only}" + (f"!{not_suffix}" if not_suffix else "")


# A type, as the resolver knows it: ("local", T) a type named T of the file's
# own package; ("pkg", directories, T or None) a type of an imported in-repo
# package, which the directories can hold (None: some type of it, such as
# what its function returned); ("ext",) a builtin or external type, whose
# methods link nothing.
GoType = tuple


class _GoResolver:
    """Resolves one Go file's call sites to path pins: see the module
    docstring's tiers."""

    def __init__(self, root: Node, source: bytes, file_path: str, config: ResolverConfig | None) -> None:
        self.source = source
        self.file_path = file_path
        self.folder = posixpath.dirname(file_path)
        package = next((c for c in root.named_children if c.type == "package_clause"), None)
        package_name = ""
        if package is not None:
            ident = next((c for c in package.named_children if c.type == "package_identifier"), None)
            package_name = _text(ident, source) if ident is not None else ""
        is_test = file_path.endswith("_test.go")
        # The files of the file's own package: an external test package
        # (`package x_test`) is its directory's other test files.
        if is_test and package_name.endswith("_test"):
            self.own = ("_test.go", "")
        elif is_test:
            self.own = (".go", "")
        else:
            self.own = (".go", "_test.go")
        # Imports: name -> its package's directory, or None for an external one.
        self.bindings: dict[str, str | None] = {}
        self.dot_dirs: list[str] = []
        self.import_pins: set[str] = set()
        unnamed: list[tuple[str, str]] = []  # (default name, directory) of in-repo unaliased imports
        for name, path in _extract_imports(root, source):
            found = config.go_package(path) if config is not None else None
            module, folder = found if found is not None else (None, None)
            if folder is not None:
                self.import_pins.add(_dir_pin(folder, ".go", "_test.go"))
            if name == "_":
                continue
            if name == ".":
                if folder is not None:
                    self.dot_dirs.append(folder)
                continue
            binding = name or _default_import_name(path, module)
            self.bindings[binding] = folder
            if name is None and folder is not None:
                unnamed.append((binding, folder))
        # The file's declarations: top-level functions (with their first
        # result type), types, each type's methods and struct fields.
        self.functions: dict[str, Node | None] = {}
        self.types: set[str] = set()
        self.methods: dict[str, set[str]] = {}
        self.fields: dict[str, dict[str, Node]] = {}
        for node in root.named_children:
            if node.type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    self.functions[_text(name_node, source)] = _first_result(node)
            elif node.type == "method_declaration":
                receiver = _receiver_type_name(node, source)
                name_node = node.child_by_field_name("name")
                if receiver and name_node is not None:
                    self.methods.setdefault(receiver, set()).add(_text(name_node, source))
            elif node.type == "type_declaration":
                for spec in node.named_children:
                    if spec.type not in ("type_spec", "type_alias"):
                        continue
                    name_node = spec.child_by_field_name("name")
                    if name_node is None:
                        continue
                    type_name = _text(name_node, source)
                    self.types.add(type_name)
                    body = spec.child_by_field_name("type")
                    if body is not None and body.type == "struct_type":
                        self.fields[type_name] = _struct_fields(body, source)
        # An unaliased in-repo import whose path name the file never uses as
        # a qualifier is used by its package clause's name instead (every
        # import must be used). An unknown qualifier (no import, type or name
        # the file declares) may be one of them, or a package-level variable
        # of another file: only when the unknown qualifiers and those imports
        # are as many are they taken to be them (resolved); otherwise an
        # unknown qualifier reaches them at package confidence (`~`).
        qualifiers: set[str] = set()
        declared: set[str] = set()

        def walk(node: Node) -> None:
            if node.type in ("selector_expression", "qualified_type"):
                operand = node.child_by_field_name("operand") or node.child_by_field_name("package")
                if operand is not None and operand.type in ("identifier", "package_identifier"):
                    qualifiers.add(_text(operand, source))
            elif node.type in ("short_var_declaration", "range_clause"):
                left = node.child_by_field_name("left")
                for name in (left.named_children if left is not None and left.type == "expression_list"
                             else [left] if left is not None else []):
                    if name.type == "identifier":
                        declared.add(_text(name, source))
            elif node.type in ("var_spec", "const_spec", "parameter_declaration",
                               "variadic_parameter_declaration"):
                declared.update(_text(name, source) for name in node.children_by_field_name("name"))
            for child in node.children:
                walk(child)

        walk(root)
        self.renamed = sorted({folder for name, folder in unnamed if name not in qualifiers})
        unknown = qualifiers - self.bindings.keys() - self.types - declared - set(self.functions)
        self.renamed_loose = len(unknown) != len(self.renamed)

    # --- pins ----------------------------------------------------------------

    def _own_dir(self, loose: bool = False) -> str:
        only, not_suffix = self.own
        return _dir_pin(self.folder, only, not_suffix, loose)

    def _package_pins(self, folder: str, loose: bool = False) -> list[str]:
        return [_dir_pin(folder, ".go", "_test.go", loose)]

    def method_pins(self, go_type: GoType, method: str) -> list[str] | None:
        """Where `method` of `go_type` can be declared; None when nowhere in
        the repository (a builtin or external type)."""
        if go_type[0] == "local":
            if method in self.methods.get(go_type[1], ()):
                return [owned_pin(self.file_path, go_type[1])]  # the type's own method
            return [self._own_dir(loose=True)]
        if go_type[0] == "pkg":
            return [pin for folder in go_type[1] for pin in self._package_pins(folder, loose=True)]
        return None

    def qualifier_dirs(self, qualifier: str) -> tuple[str, ...] | None:
        """The package directories a qualifier `q.` (not a local name) can
        name: an import's, () for an external import, the renamed imports'
        for one the file does not import by that name (`renamed`), else
        None (a package-level variable, say)."""
        if qualifier in self.bindings:
            folder = self.bindings[qualifier]
            return (folder,) if folder is not None else ()
        if self.renamed:
            return tuple(self.renamed)
        return None

    def qualifier_loose(self, qualifier: str) -> bool:
        """Whether `qualifier_dirs` reaches its directories only at package
        confidence: a renamed import an unknown qualifier may not be."""
        return qualifier not in self.bindings and self.renamed_loose

    def bare_pins(self, name: str) -> list[str] | None:
        """Where an unqualified `name()` is declared: this file, else its
        package (and any dot-imported one); None for a builtin. Wherever it
        is, it is a function, never a method of the same name (owner "")."""
        if name in self.functions:
            return [owned_pin(self.file_path, "")]
        if name in _BUILTIN_FUNCS:
            return None
        pins = [self._own_dir()]
        for folder in self.dot_dirs:
            pins += self._package_pins(folder)
        return [owned_pin(pin, "") for pin in pins]

    # --- types ---------------------------------------------------------------

    def type_of(self, node: Node | None) -> GoType | None:
        """A type expression as a GoType; None when it is not one."""
        if node is None:
            return None
        if node.type in ("pointer_type", "parenthesized_type"):
            inner = node.named_children[-1] if node.named_children else None
            return self.type_of(inner)
        if node.type == "generic_type":
            return self.type_of(node.child_by_field_name("type"))
        if node.type == "type_identifier":
            name = _text(node, self.source)
            return ("ext",) if name in _BUILTIN_TYPES else ("local", name)
        if node.type == "qualified_type":
            package = node.child_by_field_name("package")
            name = node.child_by_field_name("name")
            if package is None or name is None:
                return None
            folders = self.qualifier_dirs(_text(package, self.source))
            return ("pkg", folders, _text(name, self.source)) if folders else ("ext",)
        if node.type in ("slice_type", "array_type", "map_type", "channel_type", "function_type",
                         "interface_type", "struct_type"):
            return ("ext",)
        return None

    def value_type(self, node: Node | None, scope: dict[str, GoType | None]) -> GoType | None:
        """The type of the expression `node`, as far as this file tells."""
        if node is None:
            return None
        if node.type in ("parenthesized_expression",) and node.named_children:
            return self.value_type(node.named_children[0], scope)
        if node.type == "unary_expression":
            return self.value_type(node.child_by_field_name("operand"), scope)
        if node.type == "composite_literal":
            return self.type_of(node.child_by_field_name("type"))
        if node.type == "identifier":
            return scope.get(_text(node, self.source))
        if node.type == "selector_expression":
            return self.field_type(node, scope)
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is None:
                return None
            if func.type == "identifier":
                name = _text(func, self.source)
                if name == "new":
                    args = node.child_by_field_name("arguments")
                    return self.type_of(args.named_children[0]) if args is not None and args.named_children else None
                if name in scope:
                    return None
                if name in self.functions:
                    return self.type_of(self.functions[name])
                if name in _BUILTIN_FUNCS:
                    return ("ext",)
                # A function of the package declared elsewhere: some type of it.
                return ("local", "")
            if func.type == "selector_expression":
                operand = func.child_by_field_name("operand")
                if operand is not None and operand.type == "identifier":
                    qualifier = _text(operand, self.source)
                    if qualifier not in scope and qualifier not in self.types:
                        folders = self.qualifier_dirs(qualifier)
                        if folders is not None:
                            return ("pkg", folders, None) if folders else ("ext",)
        return None

    def field_type(self, node: Node, scope: dict[str, GoType | None]) -> GoType | None:
        """The type of `x.f` (a field of a typed value), as far as the
        struct declarations of this file tell."""
        operand = node.child_by_field_name("operand")
        field = node.child_by_field_name("field")
        if operand is None or field is None:
            return None
        if operand.type == "identifier":
            qualifier = _text(operand, self.source)
            if qualifier not in scope and qualifier not in self.types:
                # A package-level variable of another package.
                folders = self.qualifier_dirs(qualifier)
                if folders is not None:
                    return ("pkg", folders, None) if folders else ("ext",)
        owner = self.value_type(operand, scope)
        if owner is None or owner[0] != "local" or owner[1] not in self.fields:
            return None if owner is None or owner[0] != "ext" else ("ext",)
        declared = self.fields[owner[1]].get(_text(field, self.source))
        return self.type_of(declared) if declared is not None else None

    # --- calls ---------------------------------------------------------------

    def resolve(self, call: Node, scope: dict[str, GoType | None]) -> tuple[str, list[str] | None, bool] | None:
        """One call site as (callee name, its pins or None for a bare-name
        row, whether it is a member call); None when it links nothing."""
        func = call.child_by_field_name("function")
        return self.callee(func, scope) if func is not None else None

    def callee(self, func: Node, scope: dict[str, GoType | None]) -> tuple[str, list[str] | None, bool] | None:
        """`resolve` for the called expression `func` (a function value
        passed as a route's handler resolves the same way)."""
        if func.type == "parenthesized_expression" and func.named_children:
            func = func.named_children[0]
        if func.type == "identifier":
            name = _text(func, self.source)
            if name in scope:
                return None  # a function value
            pins = self.bare_pins(name)
            return (name, pins, False) if pins else None
        if func.type != "selector_expression":
            return None
        field = func.child_by_field_name("field")
        operand = func.child_by_field_name("operand")
        if field is None or operand is None:
            return None
        name = _text(field, self.source)
        if operand.type == "identifier":
            qualifier = _text(operand, self.source)
            if qualifier not in scope:
                if qualifier in self.types:
                    return name, self.method_pins(("local", qualifier), name), True
                folders = self.qualifier_dirs(qualifier)
                if folders is not None:
                    loose = self.qualifier_loose(qualifier)
                    # A package's function, never a method of the name; a
                    # qualifier that may be a variable calls any method.
                    owner = None if loose else ""
                    pins = [owned_pin(pin, owner) for folder in folders for pin in self._package_pins(folder, loose)]
                    return (name, pins, False) if pins else None
        receiver = self.value_type(operand, scope)
        if receiver is not None:
            pins = self.method_pins(receiver, name)
            return (name, pins, True) if pins else None
        if name in STOP_METHODS["go"]:
            return None
        return name, None, True


def _first_result(func: Node) -> Node | None:
    """A function declaration's (first) result type node, if any."""
    result = func.child_by_field_name("result")
    if result is not None and result.type == "parameter_list":
        first = next((c for c in result.named_children if c.type == "parameter_declaration"), None)
        return first.child_by_field_name("type") if first is not None else None
    return result


def _struct_fields(struct: Node, source: bytes) -> dict[str, Node]:
    """A struct type's named fields and their type nodes."""
    fields: dict[str, Node] = {}
    body = next((c for c in struct.named_children if c.type == "field_declaration_list"), None)
    for decl in body.named_children if body is not None else []:
        if decl.type != "field_declaration":
            continue
        type_node = decl.child_by_field_name("type")
        for child in decl.children_by_field_name("name"):
            if type_node is not None:
                fields[_text(child, source)] = type_node
    return fields


def _bind(scope: dict, names: list[Node], values: list[Node | None], resolver: _GoResolver, source: bytes,
          declared: Node | None = None) -> None:
    """Add declared names to `scope` with their types: the declared type,
    else each value's when names and values pair up, else unknown."""
    for i, name in enumerate(names):
        if name.type != "identifier":
            continue
        text = _text(name, source)
        if text == "_":
            continue
        if declared is not None:
            scope[text] = resolver.type_of(declared)
        elif len(values) == len(names):
            scope[text] = resolver.value_type(values[i], scope)
        else:
            scope[text] = None


def _function_scope(func: Node, resolver: _GoResolver, source: bytes) -> dict:
    """A function's parameters and receiver, with their types."""
    scope: dict = {}
    for field_name in ("receiver", "parameters"):
        params = func.child_by_field_name(field_name)
        for param in params.named_children if params is not None else []:
            if param.type in ("parameter_declaration", "variadic_parameter_declaration"):
                declared = param.child_by_field_name("type")
                if param.type == "variadic_parameter_declaration":
                    declared = None
                for name in param.children_by_field_name("name"):
                    text = _text(name, source)
                    if text != "_":
                        scope[text] = resolver.type_of(declared) if declared is not None else None
    return scope


def _collect_calls(
    body: Node, scope: dict, resolver: _GoResolver, source: bytes, found: list, routes: list | None = None
) -> None:
    """Walk a function body in order, binding local variables as they are
    declared and resolving each call against the scope at that point.
    Anonymous functions are walked as part of their enclosing function (no
    node of their own to attribute calls to). `routes` collects each call
    that can register a route (`_ROUTE_CALLS`) with the scope at it."""

    def expressions(node: Node | None) -> list[Node]:
        if node is None:
            return []
        return list(node.named_children) if node.type == "expression_list" else [node]

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            field = func.child_by_field_name("field") if func is not None and func.type == "selector_expression" else None
            if routes is not None and field is not None and _text(field, source) in _ROUTE_CALLS:
                routes.append((node, dict(scope)))
            # Arguments and the callee's own operand first, as evaluated.
            for child in node.children:
                walk(child)
            site = resolver.resolve(node, scope)
            if site is not None:
                found.append(site)
            return
        if node.type == "short_var_declaration":
            right = node.child_by_field_name("right")
            if right is not None:
                walk(right)
            _bind(scope, expressions(node.child_by_field_name("left")), expressions(right), resolver, source)
            return
        if node.type == "var_spec":
            value = node.child_by_field_name("value")
            if value is not None:
                walk(value)
            _bind(scope, node.children_by_field_name("name"), expressions(value), resolver, source,
                  node.child_by_field_name("type"))
            return
        if node.type == "range_clause":
            right = node.child_by_field_name("right")
            if right is not None:
                walk(right)
            _bind(scope, expressions(node.child_by_field_name("left")), [], resolver, source)
            return
        if node.type == "func_literal":
            params = node.child_by_field_name("parameters")
            for param in params.named_children if params is not None else []:
                declared = param.child_by_field_name("type")
                for name in param.children_by_field_name("name"):
                    scope[_text(name, source)] = resolver.type_of(declared) if declared is not None else None
        for child in node.children:
            walk(child)

    walk(body)


#: Calls that register a route: net/http's and chi's `Handle`/`HandleFunc`
#: (a Go 1.22 pattern, "GET /x"), gin's/echo's `GET`..., chi's and fiber's
#: `Get`... (method by name), gin's `Handle`, chi's `Method`/`MethodFunc`
#: (method as the first argument).
_ROUTE_CALLS = frozenset({
    "Handle", "HandleFunc", "Method", "MethodFunc", "Any", "GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS",
    "HEAD", "Get", "Post", "Put", "Delete", "Patch", "Options", "Head",
})
_GO_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "CONNECT", "TRACE")
_PATTERN_CALLS = ("Handle", "HandleFunc", "Method", "MethodFunc")
#: The packages whose import makes a file's route calls read: net/http and
#: the routers built on it.
_ROUTER_PACKAGES = (
    "github.com/gin-gonic/gin", "github.com/labstack/echo", "github.com/go-chi/chi", "github.com/gorilla/mux",
    "github.com/julienschmidt/httprouter", "github.com/gofiber/fiber",
)
#: A router package's constructors of a root router (`gin.Default()`,
#: `chi.NewRouter()`, `http.NewServeMux()`, `mux.NewRouter()`...).
_ROUTER_FACTORIES = frozenset({"Default", "New", "NewRouter", "NewMux", "NewServeMux"})
#: A router package's types: a root router, or a group or router that can
#: be mounted under a prefix the declaring file doesn't show.
_ROOT_ROUTER_TYPES = frozenset({"Engine", "Echo", "ServeMux", "App", "Mux", "Router"})
_SUB_ROUTER_TYPES = frozenset({"RouterGroup", "IRouter", "IRoutes", "Group", "Routes"})
#: Blocks a declaration is scoped to: one inside one that doesn't enclose the call is not its.
_SCOPE_NODES = frozenset({
    "block", "func_literal", "if_statement", "for_statement", "expression_switch_statement",
    "type_switch_statement", "select_statement", "expression_case", "type_case", "default_case",
    "communication_case",
})
_FUNCTION_NODES = ("function_declaration", "method_declaration", "func_literal")


def _descendants(node: Node, kind: str) -> list[Node]:
    found, stack = [], [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            found.append(current)
        stack.extend(reversed(current.named_children))
    return found


def _go_literal(node: Node | None, source: bytes) -> str | None:
    """A string literal's value (escapes left as written); None for
    anything else (a dynamic value)."""
    if node is None or node.type not in ("interpreted_string_literal", "raw_string_literal"):
        return None
    return _text(node, source)[1:-1]


def _go_routes(
    calls: list, root: Node, source: bytes, resolver: _GoResolver, result: ExtractionResult, file_path: str,
    repo_id: str,
) -> None:
    """Go routes as Endpoints (apis/routes.py), in a file importing net/http
    or a router package (`_ROUTER_PACKAGES`): `http.HandleFunc("GET /x", h)`
    and any `mux.Handle`/`HandleFunc` (a Go 1.22 pattern: no method maps
    every one, a host before the path is dropped, a subtree's trailing "/"
    kept; gorilla's `.Methods("GET")` read), gin's and echo's `r.GET("/x",
    h)`, chi's and fiber's `r.Get("/x", h)`, chi's `r.Method("GET", "/x",
    h)`. A route on a verb-named method counts only on a router (a router
    package's constructor or type) or with a handler that is a function or
    method (`cache.Get("/k", v)` is no route).

    The receiver is followed to its nearest declaration before the call in
    its function (then the enclosing ones, then the file's): a router
    package's constructor is a root router; a literal `Group("/x")`,
    `PathPrefix("/x").Subrouter()` or chi `Route("/x", func(r chi.Router)
    {...})` of a known router adds its prefix; a router package's group or
    router type (a parameter, a field), or anything else, is a router
    mounted where the file doesn't show (`unmounted_endpoint`). The
    handler resolves as a call to it would (`http.HandlerFunc(h)` is `h`); a
    function literal implements nothing."""
    imports = _extract_imports(root, source)
    # A router package itself (or its major version, `chi/v5`), never one of
    # its subpackages (`fiber/v2/middleware/cache`). A dot import of one is
    # not read: its names have no qualifier to tell them by.
    routers = {
        name or _default_import_name(path)
        for name, path in imports
        if name != "." and (path == "net/http" or any(
            path == each or _MAJOR_VERSION.fullmatch(path[len(each) + 1:]) and path.startswith(each + "/")
            for each in _ROUTER_PACKAGES
        ))
    }
    if not routers:
        return

    def encloses(spec: Node, function: Node, at: Node) -> bool:
        """Whether every block `spec` is scoped to, up to `function`, holds `at`."""
        node = spec.parent
        while node is not None and (node.start_byte, node.end_byte) != (function.start_byte, function.end_byte):
            if node.type in _SCOPE_NODES and not (node.start_byte <= at.start_byte and at.end_byte <= node.end_byte):
                return False
            node = node.parent
        return True

    def declaration(name: str, at: Node) -> tuple[str, Node] | None:
        """The nearest declaration of `name` before `at`: ("value", its
        value), ("type", its declared type) or ("route", the chi `Route`
        call whose function literal declares it)."""
        node = at.parent
        while node is not None:
            if node.type in _FUNCTION_NODES:
                body = node.child_by_field_name("body")
                found = None
                for spec in [*_descendants(body, "short_var_declaration"), *_descendants(body, "var_spec")] \
                        if body is not None else []:
                    if spec.end_byte > at.start_byte or not encloses(spec, node, at):
                        continue
                    if spec.type == "short_var_declaration":
                        left, right = spec.child_by_field_name("left"), spec.child_by_field_name("right")
                        names = left.named_children if left is not None else []
                        values = right.named_children if right is not None else []
                    else:
                        names, value = spec.children_by_field_name("name"), spec.child_by_field_name("value")
                        values = value.named_children if value is not None else []
                    for index, each in enumerate(names):
                        if _text(each, source) == name and (found is None or spec.start_byte > found[0]):
                            if index < len(values) and len(values) == len(names):
                                found = (spec.start_byte, ("value", values[index]))
                            elif spec.type == "var_spec" and spec.child_by_field_name("type") is not None:
                                found = (spec.start_byte, ("type", spec.child_by_field_name("type")))
                            else:
                                found = (spec.start_byte, None)
                if found is not None:
                    return found[1]
                for field in ("receiver", "parameters"):
                    params = node.child_by_field_name(field)
                    for param in params.named_children if params is not None else []:
                        if name in (_text(n, source) for n in param.children_by_field_name("name")):
                            outer = node.parent.parent if node.parent is not None else None
                            func = outer.child_by_field_name("function") if outer is not None and (
                                outer.type == "call_expression") else None
                            if node.type == "func_literal" and func is not None and func.type == "selector_expression" \
                                    and _text(func.child_by_field_name("field"), source) == "Route":
                                return ("route", outer)
                            declared = param.child_by_field_name("type")
                            return ("type", declared) if declared is not None else None
            node = node.parent
        for spec in _descendants(root, "var_spec"):
            ancestor = spec.parent
            while ancestor is not None and ancestor.type not in _FUNCTION_NODES:
                ancestor = ancestor.parent
            if ancestor is None:  # a package-level variable
                if name in (_text(n, source) for n in spec.children_by_field_name("name")):
                    declared = spec.child_by_field_name("type")
                    value = spec.child_by_field_name("value")
                    if value is not None and len(value.named_children) == 1:
                        return ("value", value.named_children[0])
                    return ("type", declared) if declared is not None else None
        return None

    def type_kind(declared: Node | None) -> tuple[str, str | None]:
        text = _text(declared, source).lstrip("*").split("[", 1)[0] if declared is not None else ""
        qualifier, _dot, last = text.rpartition(".")
        if qualifier in routers and last in _ROOT_ROUTER_TYPES:
            return ("root", "")
        if qualifier in routers and last in _SUB_ROUTER_TYPES:
            return ("unknown", None)
        return ("none", None)

    def under(parent: tuple[str, str | None], mounted: str | None) -> tuple[str, str | None]:
        if mounted is None:
            return ("skip", None)  # a dynamic prefix
        if parent[0] in ("root", "prefix"):
            return ("prefix", join_route(parent[1], mounted))
        return parent if parent[0] == "skip" else ("unknown", None)

    def kind(expr: Node, at: Node, depth: int = 0) -> tuple[str, str | None]:
        """What the receiver `expr` at `at` is: a root router ("root", ""),
        a router under a known prefix ("prefix", it), under a prefix the
        file doesn't show ("unknown"), under a dynamic one ("skip"), or not
        known to be a router ("none")."""
        if depth > 8:
            return ("none", None)
        if expr.type == "selector_expression":
            # A field of a typed value: `s.router`.
            operand, field = expr.child_by_field_name("operand"), expr.child_by_field_name("field")
            if operand is None or field is None or operand.type != "identifier":
                return ("none", None)
            found = declaration(_text(operand, source), at)
            if found is None or found[0] != "type":
                return ("none", None)
            struct = _text(found[1], source).lstrip("*")
            declared = resolver.fields.get(struct, {}).get(_text(field, source))
            return type_kind(declared) if declared is not None else ("none", None)
        if expr.type != "identifier":
            return ("none", None)
        name = _text(expr, source)
        found = declaration(name, at)
        if found is None:
            return ("root", "") if name in routers else ("none", None)
        how, node = found
        if how == "type":
            return type_kind(node)
        if how == "route":
            func = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            return under(kind(func.child_by_field_name("operand"), node, depth + 1), _go_literal(first, source))
        if node.type == "unary_expression":
            node = node.child_by_field_name("operand") or node
        if node.type in ("identifier", "selector_expression"):
            return kind(node, node, depth + 1)  # `v := s.router`
        func = node.child_by_field_name("function") if node.type == "call_expression" else None
        if func is None or func.type != "selector_expression":
            return ("none", None)
        operand, field = func.child_by_field_name("operand"), _text(func.child_by_field_name("field"), source)
        args = node.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if operand.type == "identifier" and _text(operand, source) in routers and field in _ROUTER_FACTORIES:
            return ("root", "")
        if field == "Group":
            return under(kind(operand, node, depth + 1), _go_literal(first, source))
        if field == "Subrouter" and operand.type == "call_expression":
            inner = operand.child_by_field_name("function")
            inner_args = operand.child_by_field_name("arguments")
            if inner is not None and inner.type == "selector_expression" and (
                _text(inner.child_by_field_name("field"), source) == "PathPrefix"
            ) and inner_args is not None and inner_args.named_children:
                return under(kind(inner.child_by_field_name("operand"), operand, depth + 1),
                             _go_literal(inner_args.named_children[0], source))
        if field == "With":
            return kind(operand, node, depth + 1)  # chi: the same router with middleware
        return ("none", None)

    def is_function(handler: Node, scope: dict) -> bool:
        """Whether a handler is surely a function or method: one of the
        file's, an imported package's exported name, a typed value's method."""
        if handler.type == "identifier":
            return _text(handler, source) in resolver.functions and _text(handler, source) not in scope
        if handler.type != "selector_expression":
            return handler.type == "func_literal"
        operand, field = handler.child_by_field_name("operand"), _text(handler.child_by_field_name("field"), source)
        if operand.type == "identifier" and _text(operand, source) not in scope and (
            _text(operand, source) in resolver.bindings
        ):
            return field[:1].isupper()
        receiver = resolver.value_type(operand, scope)
        return receiver is not None and receiver[0] == "local" and field in resolver.methods.get(receiver[1], ())

    for call, scope in calls:
        func = call.child_by_field_name("function")
        operand, field = func.child_by_field_name("operand"), _text(func.child_by_field_name("field"), source)
        args = call.child_by_field_name("arguments")
        values = [a for a in args.named_children if a.type != "comment"] if args is not None else []
        if len(values) < 2 or operand is None:
            continue
        methods, path, handlers, pattern = None, "", [], False
        first, second = _go_literal(values[0], source), _go_literal(values[1], source)
        if field in _PATTERN_CALLS and first in _GO_METHODS and second is not None:
            methods, path, handlers = [first], second, values[2:]  # gin's Handle, chi's Method
        elif field in ("Handle", "HandleFunc") and first is not None:
            verb, _space, rest = first.partition(" ")
            method, path = (verb, rest.strip()) if rest and verb in _GO_METHODS else ("*", first)
            path, handlers, pattern = path[path.find("/"):] if "/" in path else "", values[1:], True
            methods = [method]
            # gorilla: `r.HandleFunc("/x", h).Methods("GET", "POST")`.
            chained = call.parent
            if chained is not None and chained.type == "selector_expression" and (
                _text(chained.child_by_field_name("field"), source) == "Methods"
            ) and chained.parent is not None and chained.parent.type == "call_expression":
                listed = chained.parent.child_by_field_name("arguments")
                verbs = [_go_literal(a, source) for a in listed.named_children] if listed is not None else []
                if verbs and all(each in _GO_METHODS for each in verbs):
                    methods = sorted(set(verbs))
        elif field not in _PATTERN_CALLS and first is not None:
            methods, path, handlers = ["*" if field == "Any" else field.upper()], first, values[1:]
        if methods is None or not path.startswith("/") or not handlers or any(
            _go_literal(each, source) is not None for each in handlers
        ) or handlers[-1].type not in ("identifier", "selector_expression", "func_literal", "call_expression"):
            continue  # not a route: `client.Post("/x", "text/plain", body)`, `cache.Get("/k", &v)`
        handler = handlers[-1]
        if handler.type == "call_expression" and (wrapped := handler.child_by_field_name("arguments")) is not None:
            wrapper = handler.child_by_field_name("function")
            if wrapper is not None and _text(wrapper, source).endswith("HandlerFunc") and len(wrapped.named_children) == 1:
                handler = wrapped.named_children[0]  # http.HandlerFunc(h)
        receiver = kind(operand, call)
        if receiver[0] == "skip":
            continue  # a dynamic prefix
        if field not in _PATTERN_CALLS and receiver[0] == "none" and (
            not is_function(handler, scope) or (
                operand.type == "identifier" and _text(operand, source) in resolver.bindings
                and declaration(_text(operand, source), call) is None
            )
        ):
            continue  # `kv.Put("/config/a", value)`, another package's `cache.Get("/k", load)`
        mounted = receiver[0] in ("root", "prefix")
        full = join_route(receiver[1] or "", path)
        if pattern and path.endswith("/") and full != "/":
            full += "/"  # a ServeMux subtree, apart from the exact path
        target = resolver.callee(handler, scope) if handler.type in ("identifier", "selector_expression") else None
        for method in methods:
            if handler.type not in ("identifier", "selector_expression"):
                add_route(result, repo_id, file_path, "Go", method, full, mounted=mounted)
            elif target is None:
                name = handler if handler.type == "identifier" else handler.child_by_field_name("field")
                add_route(result, repo_id, file_path, "Go", method, full, _text(name, source), pins=set(),
                          mounted=mounted)
            else:
                name, pins, _member = target
                add_route(result, repo_id, file_path, "Go", method, full, name, pins=set(pins or ()),
                          mounted=mounted)


def extract_go_file(
    source_code: str,
    file_path: str,
    repo_id: str,
    config: ResolverConfig | None = None,
) -> ExtractionResult:
    """Parse a Go file and extract nodes and relationships.

    Args:
        source_code: The Go source code as a string.
        file_path: The Module node's identity — the file's path relative to
            the repo root, using forward slashes (e.g. 'pkg/service/main.go').
        repo_id: Repository ID for scoping nodes.
        config: The repository's resolver configuration (its go.mod and
            go.work files), read once per batch: an import path resolves by
            the longest in-repository module path it is under. None makes
            every import external.

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

    resolver = _GoResolver(root, source_bytes, file_path, config)
    result.relationships += import_rows(file_path, repo_id, set(), resolver.import_pins)
    sites = CallSites()

    def _emit_contains(parent_label: str, parent_name: str, child_label: str, child_name: str) -> None:
        # A type and a function are top-level, owned by nothing; a method by
        # its receiver's type.
        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name,
                rel_type="CONTAINS",
                to_label=child_label,
                to_name=child_name,
                repo_id=repo_id,
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
                from_owner="" if parent_label == "Class" else None,
                to_owner=parent_name if parent_label == "Class" else "",
            )
        )

    route_calls: list = []

    def _record_calls(node: Node, body: Node, caller_name: str, caller_class: str | None) -> None:
        found: list = []
        _collect_calls(body, _function_scope(node, resolver, source_bytes), resolver, source_bytes, found, route_calls)
        for name, pins, member in found:
            sites.add("Function", caller_name, caller_class, name, pins, member, caller_class or "")

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
            "owner": "",
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
                            from_owner="",
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
            "owner": "",
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
            _record_calls(node, body_node, func_name, None)

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
            "owner": receiver_type or "",
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
            _record_calls(node, body_node, method_name, receiver_type)

    for node in root.named_children:
        if node.type == "type_declaration":
            for type_spec in node.named_children:
                if type_spec.type == "type_spec":
                    _visit_struct(type_spec)
        elif node.type == "function_declaration":
            _visit_function(node)
        elif node.type == "method_declaration":
            _visit_method(node)
    result.relationships += sites.rows(file_path, repo_id)
    _go_routes(route_calls, root, source_bytes, resolver, result, file_path, repo_id)

    return own_edges(result, file_path)


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
            node is keyed by file_path's path relative to repo_root, and the
            repository's go.mod and go.work files resolve its imports. When
            omitted, falls back to the bare filename and every import is
            external.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8")

    config = None
    if repo_root is not None:
        repo_root = Path(repo_root)
        try:
            rel = file_path.resolve().relative_to(repo_root.resolve())
            module_name = rel.as_posix()
        except ValueError:
            module_name = file_path.name
        config = ResolverConfig(repo_root)
    else:
        module_name = file_path.name

    result = extract_go_file(source_code, module_name, repo_id, config)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
