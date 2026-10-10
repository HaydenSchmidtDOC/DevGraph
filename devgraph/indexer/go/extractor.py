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

from devgraph.indexer.calls import STOP_METHODS, CallSites, import_rows
from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    own_edges,
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


def _default_import_name(import_path: str) -> str:
    """The name an unaliased import is used by, as far as its path tells:
    the last path element, skipping a major-version element (`chi/v5` is
    `chi`) and a gopkg.in-style `.vN` (`yaml.v3` is `yaml`). A package
    clause may name it otherwise (`legacydb/` declaring `package db`); see
    `_GoResolver.unknown_qualifier`."""
    parts = [part for part in import_path.split("/") if part]
    if len(parts) > 1 and _MAJOR_VERSION.fullmatch(parts[-1]):
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
            folder = config.go_package_dir(path) if config is not None else None
            if folder is not None:
                self.import_pins.add(_dir_pin(folder, ".go", "_test.go"))
            if name == "_":
                continue
            if name == ".":
                if folder is not None:
                    self.dot_dirs.append(folder)
                continue
            binding = name or _default_import_name(path)
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
        # import must be used): an unknown qualifier may be any of them.
        qualifiers = set()

        def walk(node: Node) -> None:
            if node.type in ("selector_expression", "qualified_type"):
                operand = node.child_by_field_name("operand") or node.child_by_field_name("package")
                if operand is not None and operand.type in ("identifier", "package_identifier"):
                    qualifiers.add(_text(operand, source))
            for child in node.children:
                walk(child)

        walk(root)
        self.renamed = sorted({folder for name, folder in unnamed if name not in qualifiers})

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
                return [self.file_path]
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

    def bare_pins(self, name: str) -> list[str] | None:
        """Where an unqualified `name()` is declared: this file, else its
        package (and any dot-imported one); None for a builtin."""
        if name in self.functions:
            return [self.file_path]
        if name in _BUILTIN_FUNCS:
            return None
        pins = [self._own_dir()]
        for folder in self.dot_dirs:
            pins += self._package_pins(folder)
        return pins

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
        if func is None:
            return None
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
                    pins = [pin for folder in folders for pin in self._package_pins(folder)]
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


def _collect_calls(body: Node, scope: dict, resolver: _GoResolver, source: bytes, found: list) -> None:
    """Walk a function body in order, binding local variables as they are
    declared and resolving each call against the scope at that point.
    Anonymous functions are walked as part of their enclosing function (no
    node of their own to attribute calls to)."""

    def expressions(node: Node | None) -> list[Node]:
        if node is None:
            return []
        return list(node.named_children) if node.type == "expression_list" else [node]

    def walk(node: Node) -> None:
        if node.type == "call_expression":
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
            )
        )

    def _record_calls(node: Node, body: Node, caller_name: str, caller_class: str | None) -> None:
        found: list = []
        _collect_calls(body, _function_scope(node, resolver, source_bytes), resolver, source_bytes, found)
        for name, pins, member in found:
            sites.add("Function", caller_name, caller_class, name, pins, member)

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
