"""Tree-sitter-based C++ source-code extractor.

Handles both C++ source files (.cpp/.cc/.cxx) and headers (.h/.hpp) with one
extractor — headers and sources share the same grammar, and the same
class/function/include shapes, so there is no reason to split them.

Resolution (see docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md,
C++). `#include` resolution has no reliable general algorithm without the
build (compiler `-I` flags, vcpkg/conan layouts), so a file's quoted
includes are read as the compiler's search would most likely go:

  - `#include "x/y.h"` names the file beside the includer (`<dir>/x/y.h`,
    resolved) and, by suffix, `/x/y.h` anywhere else (package, vendored
    copies left out); a bare `"y.h"` only under a conventional include root
    (`/include/y.h`, `/src/y.h`), so two `log.h` in different
    folders stay apart. `#include <system.h>` is not resolved at all.
  - A header's declarations are defined in its same-stem source (`.cpp`
    or `.cc`, never `.cxx`), beside the header the include names or under
    a `src/` root (`/src/x/y.cpp`; package), which an include reaches too.
    A header outside the includer's folder, `include/` and `src/` is not
    resolved.

CALLS, by the first tier that applies: a function defined in the file ->
this file; `ns::f()` or `Cls::f()` -> the includes whose path names the
qualifier (`util::trim` -> `util/strings.h` and its source), else every
include; `x.m()` / `x->m()` on a typed receiver (a parameter, a local, `auto
x = new T`, a field of a class declared in the file) -> this file for a
class declared here with m, else the include named after the type or its
namespace (`Inventory` -> `inventory.h`, `net::Connection` -> `net/conn.h`),
else the type's conventional files (`/inventory.h`, `/inventory.cpp`) and
every include; any other `f()` or `this->m()` -> every include. `std::` and
an STL type (calls.STOP_TYPES["cpp"]) link nothing, nor does a call when
the file includes nothing; an untyped receiver keeps a bare-name edge that
never ends at its caller, unless its method is stoplisted
(calls.STOP_METHODS["cpp"]). A declaration (a prototype) is no Function
node, so only definitions are targets.

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
Function's CONTAINS edge has a real endpoint to MATCH onto. The stub is
file-less and carries `source`, so it is a shared node the file claims
(GraphEngine._claim_nodes_tx), like a route's handler stub: it never merges
onto the header's own Class, whichever file is indexed first, and it goes
with its last claimant.
"""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import NamedTuple

import tree_sitter_cpp as tscpp
from tree_sitter import Language, Node, Parser

from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, CallSites, import_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship, own_edges

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


# --- resolution ------------------------------------------------------------------

_HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx")
_SOURCE_EXTS = (".cpp", ".cc")
#: Where a quoted include of a bare name (`"platform.h"`) is looked for
#: besides the includer's directory: the conventional include roots.
_INCLUDE_ROOTS = ("include", "src")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


class _Include(NamedTuple):
    """One quoted include: the file beside the includer (resolved, None
    when it leaves the repository), its suffix pins elsewhere (package),
    the pins of the header's same-stem sources (package), and the lowercase
    names of its path (directories and stem), which a qualifier or a type
    name can pick it by."""

    beside: str | None
    suffixes: tuple[str, ...]
    sources: tuple[str, ...]
    names: tuple[str, ...]

    def pins(self) -> list[str]:
        return ([self.beside] if self.beside else []) + list(self.suffixes) + list(self.sources)


def _include(spelling: str, current_dir: str) -> _Include:
    joined = posixpath.normpath(f"{current_dir}/{spelling}" if current_dir else spelling)
    beside = None if joined == ".." or joined.startswith("../") else joined
    clean = spelling[2:] if spelling.startswith("./") else spelling
    parts = clean.split("/")
    suffixes: tuple[str, ...] = ()
    if ".." not in parts:
        if len(parts) > 1:
            suffixes = (f"/{clean}^",)
        else:
            suffixes = tuple(f"/{root}/{clean}^" for root in _INCLUDE_ROOTS)
    sources: tuple[str, ...] = ()
    if posixpath.splitext(clean)[1] in _HEADER_EXTS:
        # Beside the header the include names, at package confidence (a
        # directory pin limited to the source's name), and under a src/
        # root, as the header's own include roots go.
        if beside:
            folder, stem = posixpath.dirname(beside) or ".", posixpath.splitext(posixpath.basename(beside))[0]
            sources += tuple(f"{folder if folder == '.' else folder + '/.'}~?{stem}{each}" for each in _SOURCE_EXTS)
        if ".." not in parts:
            stem_path = posixpath.splitext(clean)[0]
            sources += tuple(f"/src/{stem_path}{each}^" for each in _SOURCE_EXTS)
    names = tuple(part.lower() for part in posixpath.splitext(clean)[0].split("/") if part not in ("", ".", ".."))
    return _Include(beside, suffixes, sources, names)


def _variants(name: str) -> set[str]:
    """The lowercase spellings a file named after `name` can have
    (`HttpClient`: httpclient, http_client)."""
    return {name.lower(), _CAMEL.sub("_", name).lower()}


class _CppType(NamedTuple):
    name: str
    qualifier: tuple[str, ...] = ()

    @property
    def library(self) -> bool:
        return self.qualifier[:1] == ("std",) or self.name in STOP_TYPES["cpp"]


def _cpp_type(node: Node | None, source: bytes) -> _CppType | None:
    """A declared type as the class its members are on (`const
    net::Connection&`: Connection in net); None for a primitive or `auto`."""
    if node is None:
        return None
    if node.type == "type_identifier":
        return _CppType(_text(node, source))
    if node.type == "template_type":
        return _cpp_type(node.child_by_field_name("name"), source)
    if node.type == "qualified_identifier":
        scope = node.child_by_field_name("scope")
        inner = _cpp_type(node.child_by_field_name("name"), source)
        if inner is None:
            return None
        head = (_text(scope, source),) if scope is not None else ("",)
        return _CppType(inner.name, head + inner.qualifier)
    return None


def _declared_name(node: Node | None, source: bytes) -> str | None:
    """The variable a declarator declares (`*p`, `&r`, `x = 1`, `x(1)`)."""
    while node is not None and node.type not in ("identifier", "field_identifier"):
        inner = node.child_by_field_name("declarator")
        node = inner if inner is not None else next(
            (c for c in node.named_children if c.type in ("identifier", "field_identifier")), None)
    return _text(node, source) if node is not None else None


class _CppResolver:
    """What one C++ file's names refer to: its quoted includes, the
    functions it defines and the classes it declares (methods and fields)."""

    def __init__(self, root: Node, source: bytes, file_path: str) -> None:
        self.source = source
        self.file_path = file_path
        current_dir = posixpath.dirname(file_path)
        self.includes: list[_Include] = []
        self.functions: set[str] = set()
        self.methods: dict[str, set[str]] = {}
        self.fields: dict[str, dict[str, _CppType | None]] = {}
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "preproc_include":
                path = next((c for c in node.named_children if c.type == "string_literal"), None)
                content = next((c for c in path.named_children if c.type == "string_content"), None) \
                    if path is not None else None
                if content is not None:
                    self.includes.append(_include(_text(content, source), current_dir))
            elif node.type == "function_definition":
                name = _function_name(node, source)
                if name:
                    self.functions.add(name)
            elif node.type in ("class_specifier", "struct_specifier"):
                self._class(node)
            stack.extend(reversed(node.named_children))
        own_stem, own_ext = posixpath.splitext(file_path)
        self.own_sources = [f"/{own_stem}{each}^" for each in _SOURCE_EXTS] if own_ext in _HEADER_EXTS else []

    def _class(self, node: Node) -> None:
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is None or body is None:
            return
        cls = _text(name, self.source)
        methods = self.methods.setdefault(cls, set())
        fields = self.fields.setdefault(cls, {})
        for member in body.named_children:
            if member.type == "function_definition":
                method = _function_name(member, self.source)
                if method:
                    methods.add(method)
            elif member.type in ("field_declaration", "declaration"):
                declarator = member.child_by_field_name("declarator")
                if declarator is not None and declarator.type == "function_declarator":
                    method = _declared_name(declarator.child_by_field_name("declarator"), self.source)
                    if method:
                        methods.add(method)
                else:
                    field = _declared_name(declarator, self.source)
                    if field:
                        fields[field] = _cpp_type(member.child_by_field_name("type"), self.source)

    def included(self) -> list[str]:
        return [pin for each in self.includes for pin in each.pins()]

    def named(self, names: list[str]) -> list[str] | None:
        """The pins of the includes whose path names one of `names` (a
        namespace or class qualifier, a type), None when none does."""
        wanted = set().union(*(_variants(name) for name in names if name)) if names else set()
        pins = [pin for each in self.includes if wanted & set(each.names) for pin in each.pins()]
        return pins or None

    def type_pins(self, typed: _CppType, method: str) -> list[str] | None:
        """Where a method of `typed` is: this file for a class declared
        here (and its same-stem sources, for a header), else the include
        named after the type or its namespace, else the type's conventional
        files and every include; None for a library type."""
        if typed.library:
            return None
        if typed.name in self.methods and (method in self.methods[typed.name] or method in self.functions):
            return [self.file_path] + self.own_sources
        found = self.named([typed.name, *typed.qualifier])
        if found:
            return found
        conventional = [
            f"/{variant}{ext}^" for variant in sorted(_variants(typed.name) | {typed.name})
            for ext in (".h", ".hpp", ".cpp", ".cc")
        ]
        return conventional + self.included()


def _function_name(node: Node, source: bytes) -> str | None:
    """A function definition's name (`Inventory::add`: add)."""
    declarator = _unwrap_declarator(node.child_by_field_name("declarator"))
    if declarator is None or declarator.type != "function_declarator":
        return None
    inner = declarator.child_by_field_name("declarator")
    if inner is None:
        return None
    return _text(inner, source).split("::")[-1]


class _CppCalls:
    """One caller's call sites, resolved by the tiers (see the module
    docstring): (callee name, pins or None for a bare name, member call?)."""

    def __init__(self, resolver: _CppResolver, cls: str | None, variables: dict[str, _CppType | None]) -> None:
        self.resolver = resolver
        self.cls = cls
        self.variables = variables
        self.found: list[tuple[str, list[str] | None, bool]] = []

    def walk(self, node: Node) -> None:
        source = self.resolver.source
        if node.type in ("function_definition", "class_specifier", "struct_specifier"):
            return
        if node.type == "declaration":
            declared = _cpp_type(node.child_by_field_name("type"), source)
            for declarator in node.children_by_field_name("declarator"):
                name = _declared_name(declarator, source)
                value = declarator.child_by_field_name("value")
                typed = declared
                if declared is None and value is not None and value.type == "new_expression":
                    typed = _cpp_type(value.child_by_field_name("type"), source)
                if name:
                    self.variables[name] = typed
        elif node.type == "call_expression":
            self.call(node)
        for child in node.children:
            self.walk(child)

    def _field(self, name: str) -> _CppType | None:
        return self.resolver.fields.get(self.cls or "", {}).get(name)

    def _bare(self, name: str) -> None:
        if name not in STOP_METHODS["cpp"]:
            self.found.append((name, None, True))

    def call(self, node: Node) -> None:
        source = self.resolver.source
        resolver = self.resolver
        function = node.child_by_field_name("function")
        if function is not None and function.type == "template_function":
            function = function.child_by_field_name("name")
        if function is None:
            return
        if function.type == "identifier":
            name = _text(function, source)
            if name in self.variables:
                return  # a function pointer or a lambda
            pins = [resolver.file_path] if name in resolver.functions else resolver.included()
            if pins:
                self.found.append((name, pins, False))
            return
        if function.type == "qualified_identifier":
            segs = _text(function, source).split("::")
            segs = [seg.split("<", 1)[0].strip() for seg in segs]
            name = segs[-1]
            if segs[0] == "std" or not name:
                return
            if name in resolver.functions:
                pins = [resolver.file_path]
            else:
                pins = resolver.named([seg for seg in segs[:-1] if seg]) or resolver.included()
            if pins:
                self.found.append((name, pins, False))
            return
        if function.type != "field_expression":
            return
        field = function.child_by_field_name("field")
        argument = function.child_by_field_name("argument")
        if field is None or argument is None:
            return
        name = _text(field, source)
        if argument.type == "this":
            if name in resolver.functions:
                pins = [resolver.file_path]
            else:
                pins = resolver.named([self.cls] if self.cls else []) or resolver.included()
            if pins:
                self.found.append((name, pins, False))
            return
        typed = None
        if argument.type == "identifier":
            text = _text(argument, source)
            typed = self.variables[text] if text in self.variables else self._field(text)
        elif argument.type == "field_expression":
            owner = argument.child_by_field_name("argument")
            member = argument.child_by_field_name("field")
            if owner is not None and owner.type == "this" and member is not None:
                typed = self._field(_text(member, source))
        if typed is None:
            self._bare(name)
            return
        pins = resolver.type_pins(typed, name)
        if pins:
            self.found.append((name, pins, False))


def _function_variables(node: Node, source: bytes) -> dict[str, _CppType | None]:
    """A function definition's parameters by name, with their types."""
    variables: dict[str, _CppType | None] = {}
    declarator = _unwrap_declarator(node.child_by_field_name("declarator"))
    params = declarator.child_by_field_name("parameters") if declarator is not None else None
    for param in params.named_children if params is not None else []:
        name = _declared_name(param.child_by_field_name("declarator"), source)
        if name:
            variables[name] = _cpp_type(param.child_by_field_name("type"), source)
    return variables


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

    resolver = _CppResolver(root, source_bytes, file_path)
    result.relationships += import_rows(
        file_path, repo_id,
        {each.beside for each in resolver.includes if each.beside},
        {pin for each in resolver.includes for pin in each.suffixes},
    )
    sites = CallSites()

    def _record_calls(body: Node, caller_label: str, caller_name: str, cls: str | None, variables: dict) -> None:
        collector = _CppCalls(resolver, cls, variables)
        collector.walk(body)
        for name, pins, member in collector.found:
            sites.add(caller_label, caller_name, cls, name, pins, member)

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
                properties={"type": "class", "source": file_path},
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
                _record_calls(node, parent_label, parent_name if parent_name else file_path, caller_class, {})

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
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
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
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
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
                from_file=file_path if contains_label == "Class" else None,
                to_file=file_path,
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            _record_calls(body_node, "Function", func_name, class_name, _function_variables(node, source_bytes))

    visit_block(root, None, "Module")
    result.relationships += sites.rows(file_path, repo_id)

    return own_edges(result, file_path)


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
