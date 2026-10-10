"""Tree-sitter-based Rust source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape and philosophy
(Implementation Plan #8, row 5): parse with Tree-sitter, walk the tree, and
emit `GraphNode`/`GraphRelationship` records via the shared
`devgraph.indexer.common` dataclasses, with CALLS and IMPORTS resolved
through crate roots, module paths and types (see Resolution below): a
candidate target that doesn't correspond to a real indexed node just never
becomes an edge, since `upsert_relationship` only MATCH-links existing
nodes.

Rust has no direct equivalent of Python's `class`/`def`, so this extractor
makes a few judgment calls, documented here since they're not obvious from
the graph schema alone:

- `struct`, `enum`, and `trait` items all map to the graph's generic
  `Class` label -- the closest structural equivalent DevGraph's schema has
  to "a named, inheritable type/interface". `properties["kind"]` records
  which one it was ('struct' | 'enum' | 'trait') for callers that care.
- `impl Trait for Type` -> an EXTENDS edge from Class(Type) to
  Class(Trait), the same direction Python's subclass-EXTENDS-base uses (the
  Type "inherits"/implements the Trait's contract). `impl Type { .. }`
  (an inherent impl, no trait) attributes its methods to Class(Type) the
  same way but emits no EXTENDS edge, since there's no trait to point at.
- Free (module-level) `fn` items map to Function, CONTAINS from Module.
  `impl`-block methods (both trait impls and inherent impls) are attributed
  to the struct/enum they're implemented for, the same way Python attributes
  methods to their enclosing class rather than to the module.
- A trait's *default* methods (a `function_item` with a body, inside the
  trait's own declaration_list) are attributed to the trait's Class node.
  Signature-only trait methods (`fn foo(&self);` -- no body, a
  `function_signature_item` node, not `function_item`) are NOT extracted as
  Function nodes, mirroring how the Python extractor only extracts actual
  definitions, never bare declarations.
- Inline `mod foo { .. }` blocks are flattened into their enclosing scope
  for nodes (a struct declared inside an inline `mod` is a Class of the
  file like any other), but paths inside one resolve from module foo
  (`super::` there is the file's own module).

Resolution (see docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md,
Rust). A configured library's root file (a `[lib] path` included) and a
file beside a package's Cargo.toml (`build.rs`) are crate roots; otherwise
a file's crate root is a library root's directory above it, else the
nearest `src/` (`src/bin/x.rs` and `src/bin/x/main.rs` are crate roots of
their own), else the nearest `tests/`, `benches/` or `examples/` (each file
directly in one, and each `<dir>/<x>/main.rs`, is a crate root), else its
own directory; its module path follows from there
(`src/a/b.rs` is `a::b`, `src/a/mod.rs` is `a`, `lib.rs`/`main.rs` the
root). A module `a::b` is the file `<root>/a/b.rs` or `<root>/a/b/mod.rs`,
or this file for one of its inline modules; another workspace crate's name
(its Cargo.toml, the resolver configuration) names that crate's library
by its exact files: resolved when no other package has the name, else at
package confidence.

- IMPORTS: `mod foo;` names the child module of the file's own (the 2018
  rule: `mod user;` in `src/models.rs` is `src/models/user.rs`); a `use`
  path names the module of its item (`use a::b::Type` is a::b; a lowercase
  name is the module a::b::x when the file writes `x::`, the item's module
  a::b when it calls `x(`, else either). `crate::`, `self::` and `super::`
  are followed; std and any external crate give no edge.
- CALLS, by the first tier that applies: `f()` declared in the file -> this
  file; a `use` binding or a path (`m::f()`, `crate::m::f()`,
  `shop_core::f()`, `Type::f()`) -> its module's files (and the recursive
  prefix of its directory, package: an item re-exported from a submodule),
  `Type::f()` on a type declared here -> this file; `self.m()` and a method
  on a typed receiver (a parameter, `let x: T`, `let x = T::new()` or
  `T { .. }`, a field of a struct declared here, the element of a `for`
  loop over a `Vec<T>` or slice) -> this file when the type and the method
  are declared here, else the type's module; a std type (calls.STOP_TYPES)
  or a path outside the repository links nothing, and neither does a bare
  `f()` nothing declares or imports; an untyped receiver keeps a bare-name
  edge that never ends at its caller, unless its method is stoplisted
  (calls.STOP_METHODS["rust"]). The arguments of a macro invocation
  (`println!("{}", f(x))`) are reparsed for their calls. `#[path]`
  attributes are not read.

Known limitations (documented per the Implementation Plan's per-language
table):
- A method inherited through a trait implemented in another file is not
  found through a typed receiver of the implementing type; such a call
  keeps a bare-name edge.
- Trait supertraits (`trait A: B`) do not emit an EXTENDS edge -- only
  `impl Trait for Type` does, per the Implementation Plan's brief.
"""

from __future__ import annotations

import logging
import posixpath
import re
from pathlib import Path
from typing import NamedTuple

import tree_sitter_rust as tsrust
from tree_sitter import Language, Node, Parser

from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, CallSites, import_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship, own_edges
from devgraph.indexer.resolver_config import ResolverConfig

logger = logging.getLogger(__name__)

_RUST_LANGUAGE = Language(tsrust.language())

# Node types that introduce a new definition scope; call-target walking must
# not descend into these when scanning an enclosing body, so calls get
# attributed to the correct (innermost) definition, not hoisted outward --
# mirrors the Python extractor's function_definition/class_definition stop.
_SCOPE_BOUNDARY_TYPES = (
    "function_item",
    "impl_item",
    "trait_item",
    "struct_item",
    "enum_item",
    "mod_item",
)


def _make_parser() -> Parser:
    return Parser(_RUST_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """First line up to a blank line or terminating period, matching the
    summary convention the Python extractor uses for `description`."""
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _leading_doc_comment(node: Node, source: bytes) -> str | None:
    """Collect `///` line comments (or a `/** */` block comment) immediately
    preceding `node`, Rust's *outer* doc-comment convention -- the closest
    equivalent to Python's leading-string-literal docstring.

    Deliberately excludes `//!`/`/*!` *inner* doc comments: those document
    the *enclosing* item (typically the module -- see `_extract_module_doc`)
    rather than whatever item happens to follow them, so treating them as
    this item's own doc would misattribute a module-level doc comment to
    the first struct/fn that happens to follow it in the file.
    """
    lines: list[str] = []
    sib = node.prev_sibling
    while sib is not None:
        if sib.type == "line_comment":
            text = _text(sib, source)
            if text.startswith("///") and not text.startswith("////"):
                lines.insert(0, text[3:].strip())
                sib = sib.prev_sibling
                continue
            break
        if sib.type == "block_comment":
            text = _text(sib, source)
            if text.startswith("/**") and not text.startswith("/***"):
                inner = text[3:]
                if inner.endswith("*/"):
                    inner = inner[:-2]
                lines.insert(0, inner.strip())
                sib = sib.prev_sibling
                continue
            break
        break
    return "\n".join(lines) if lines else None


def _extract_module_doc(root: Node, source: bytes) -> str | None:
    """Collect leading `//!`/`/*! */` inner doc comments at the very top of
    the file -- Rust's module-doc convention (there's no leading-string-
    literal equivalent to attach a Module docstring to, unlike Python)."""
    lines: list[str] = []
    for child in root.children:
        if child.type == "line_comment":
            text = _text(child, source)
            if text.startswith("//!"):
                lines.append(text[3:].strip())
                continue
            break
        if child.type == "block_comment":
            text = _text(child, source)
            if text.startswith("/*!"):
                inner = text[3:]
                if inner.endswith("*/"):
                    inner = inner[:-2]
                lines.append(inner.strip())
                continue
            break
        break
    return "\n".join(lines) if lines else None


def _type_name(node: Node | None, source: bytes) -> str | None:
    """Reduce a type expression node to a simple type name, unwrapping the
    generic/reference/scoped-path shapes that can wrap a struct/enum/trait
    reference (e.g. `Foo<T>`, `&Foo`, `pkg::Foo`)."""
    if node is None:
        return None
    if node.type in ("type_identifier", "identifier"):
        return _text(node, source)
    if node.type in ("generic_type", "reference_type"):
        inner = node.child_by_field_name("type")
        return _type_name(inner, source)
    if node.type == "scoped_type_identifier":
        name = node.child_by_field_name("name")
        return _text(name, source) if name is not None else None
    return _text(node, source)


# --- resolution ------------------------------------------------------------------

#: Directories whose files are crate roots of their own (S9): an integration
#: test, a benchmark, an example.
_OWN_ROOT_DIRS = ("tests", "benches", "examples")
#: Generic types a method call sees through (auto-deref).
_SMART_POINTERS = ("Box", "Rc", "Arc")
#: Generic containers a `for` loop takes its element type from.
_CONTAINERS = ("Vec", "VecDeque", "HashSet", "BTreeSet", "Option", "LinkedList", "BinaryHeap")


class _Crate(NamedTuple):
    """A crate: its root directory, the files its root module can be, and,
    for one reached by another crate's name (Cargo configuration), whether
    another package shares that name (its files then at package
    confidence)."""

    root: str
    root_files: tuple[str, ...]
    cross: bool = False
    shared: bool = False


class _Module(NamedTuple):
    crate: _Crate
    path: tuple[str, ...]


class _Type(NamedTuple):
    """A type as its path segments (`("crate", "net", "Client")`), and the
    element type a `for` loop over it yields."""

    segs: tuple[str, ...]
    element: "_Type | None" = None


def _join(*parts: str) -> str:
    return "/".join(part for part in parts if part)


def _layout(
    file_path: str, lib_files: frozenset[str] = frozenset(), packages: frozenset[str] = frozenset(),
) -> tuple[_Crate, tuple[str, ...]]:
    """The crate a file belongs to and the file's module path in it.
    `lib_files` are the configured libraries' root files (a `[lib] path`
    included) and `packages` the directories holding a package's
    Cargo.toml. A library root file is its crate's root, and so is a file
    beside Cargo.toml (`build.rs`); a file under a library root's directory
    belongs to that crate. Otherwise the root is the nearest `src/` above
    the file (`src/bin/x.rs` and `src/bin/x/main.rs` are crate roots of
    their own), else the nearest `tests/`, `benches/` or `examples/` (each
    file directly in one, and each `<dir>/<x>/main.rs`, is a crate root),
    else the file's own directory."""
    *dirs, name = file_path.split("/")
    stem = name[:-3] if name.endswith(".rs") else name
    folder = _join(*dirs)
    if file_path in lib_files or folder in packages or (not dirs and "" in packages):
        return _Crate(folder, (file_path,)), ()
    src = max((i for i, d in enumerate(dirs) if d == "src"), default=None)
    own = max((i for i, d in enumerate(dirs) if d in _OWN_ROOT_DIRS), default=None)
    # The nearest library root directory above the file, when no src/ is nearer.
    lib = max(
        ((posixpath.dirname(each).count("/") + 1 if posixpath.dirname(each) else 0, each) for each in lib_files
         if posixpath.dirname(each) and file_path.startswith(posixpath.dirname(each) + "/")),
        default=None,
    )
    if lib is not None and lib[0] > (src + 1 if src is not None else -1):
        root = posixpath.dirname(lib[1])
        crate = _Crate(root, (lib[1],))
        rest = dirs[lib[0]:]
    elif src is not None and src + 1 < len(dirs) and dirs[src + 1] == "bin":
        if len(dirs) == src + 2:
            return _Crate(folder, (file_path,)), ()
        root = _join(*dirs[: src + 3])
        crate = _Crate(root, (f"{root}/main.rs",))
        rest = dirs[src + 3:]
    elif src is not None:
        root = _join(*dirs[: src + 1])
        crate = _Crate(root, (f"{root}/lib.rs", f"{root}/main.rs"))
        rest = dirs[src + 1:]
    elif own is not None:
        if len(dirs) == own + 1 or (len(dirs) == own + 2 and name == "main.rs"):
            return _Crate(folder, (file_path,)), ()
        root = _join(*dirs[: own + 1])
        crate = _Crate(root, ())
        rest = dirs[own + 1:]
    else:
        root = folder
        crate = _Crate(root, (_join(root, "lib.rs"), _join(root, "main.rs")))
        rest = []
    if not rest and file_path in crate.root_files:
        return crate, ()
    return crate, tuple(rest) + (() if stem == "mod" else (stem,))


def _segs(node: Node | None, source: bytes) -> list[str] | None:
    """A path (`a::b::C`, `super::x`, `Vec::<T>`, `g::<T>`) as its segments,
    or None when it is no plain path. A path from the global root (`::std`)
    starts with ""."""
    if node is None:
        return None
    if node.type in ("identifier", "type_identifier", "self", "super", "crate", "field_identifier"):
        return [_text(node, source)]
    if node.type in ("scoped_identifier", "scoped_type_identifier"):
        path = node.child_by_field_name("path")
        name = node.child_by_field_name("name")
        if name is None:
            return None
        head = [""] if path is None else _segs(path, source)
        return None if head is None else head + [_text(name, source)]
    if node.type == "generic_type":
        return _segs(node.child_by_field_name("type"), source)
    if node.type == "generic_function":
        return _segs(node.child_by_field_name("function"), source)
    return None


def _uppercase(seg: str) -> bool:
    return seg[:1].isupper()


class _RustResolver:
    """What one Rust file's names refer to: its crate and module path, its
    `use` bindings, `mod` declarations and inline modules (per module
    scope), the types and functions it declares, and the workspace's crates
    (resolver configuration)."""

    def __init__(self, root: Node, source: bytes, file_path: str, config: ResolverConfig | None) -> None:
        self.source = source
        self.file_path = file_path
        self.crates = config.rust_crates() if config is not None else {}
        packages = frozenset(config.rust_packages()) if config is not None else frozenset()
        lib_files = frozenset(root for roots in self.crates.values() for root in roots)
        self.crate, self.module = _layout(file_path, lib_files, packages)
        # module scope -> name -> (path segments, the scope it was declared in)
        self.bindings: dict[tuple, dict[str, list[str]]] = {}
        self.globs: dict[tuple, list[list[str]]] = {}
        self.declared: dict[tuple, set[str]] = {}  # `mod x;` and inline `mod x {}` names per scope
        self.inline: set[tuple] = {self.module}
        self.types: set[str] = set()
        self.functions: set[str] = set()
        self.methods: dict[str, set[str]] = {}
        self.fields: dict[str, dict[str, _Type]] = {}
        self.uses: list[tuple[tuple, list[str], str | None, bool]] = []  # (scope, segs, binding, glob)
        self.mod_decls: list[tuple[tuple, str]] = []
        self._use_ranges: list[tuple[int, int]] = []
        self._collect(root, self.module)
        # A use's ranges blanked, for how the file uses a name (`_used_as`).
        text = bytearray(source)
        for start, end in self._use_ranges:
            text[start:end] = b" " * (end - start)
        self._code = text.decode("utf-8", errors="replace")

    # --- what the file declares -------------------------------------------------

    def _collect(self, node: Node, scope: tuple) -> None:
        for child in node.named_children:
            kind = child.type
            if kind == "use_declaration":
                self._use_ranges.append((child.start_byte, child.end_byte))
                argument = child.child_by_field_name("argument")
                if argument is not None:
                    for segs, binding, glob in self._use_paths(argument, []):
                        self.uses.append((scope, segs, binding, glob))
                        if glob:
                            self.globs.setdefault(scope, []).append(segs)
                        elif binding and binding != "_":
                            self.bindings.setdefault(scope, {})[binding] = segs
            elif kind == "mod_item":
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name is None:
                    continue
                mod = _text(name, self.source)
                self.declared.setdefault(scope, set()).add(mod)
                if body is None:
                    self.mod_decls.append((scope, mod))
                else:
                    self.inline.add(scope + (mod,))
                    self._collect(body, scope + (mod,))
            elif kind in ("struct_item", "enum_item", "trait_item", "union_item", "type_item"):
                name = child.child_by_field_name("name")
                if name is not None:
                    self.types.add(_text(name, self.source))
                if kind == "struct_item" and name is not None:
                    self._collect_fields(child, _text(name, self.source))
                if kind == "trait_item":
                    self._collect_methods(child, _text(name, self.source) if name is not None else None)
            elif kind == "impl_item":
                self._collect_methods(child, _type_name(child.child_by_field_name("type"), self.source))
            elif kind == "function_item":
                self._collect_functions(child)

    def _collect_functions(self, node: Node) -> None:
        """Every function item at or under `node` (nested ones included)."""
        if node.type == "function_item":
            name = node.child_by_field_name("name")
            if name is not None:
                self.functions.add(_text(name, self.source))
        for child in node.named_children:
            if child.type not in ("impl_item", "trait_item"):
                self._collect_functions(child)

    def _collect_methods(self, node: Node, type_name: str | None) -> None:
        body = node.child_by_field_name("body")
        for child in body.named_children if body is not None else []:
            if child.type in ("function_item", "function_signature_item"):
                name = child.child_by_field_name("name")
                if name is not None and type_name:
                    self.methods.setdefault(type_name, set()).add(_text(name, self.source))
                if child.type == "function_item":
                    self._collect_functions(child.child_by_field_name("body") or child)

    def _collect_fields(self, node: Node, struct: str) -> None:
        body = node.child_by_field_name("body")
        for field in body.named_children if body is not None and body.type == "field_declaration_list" else []:
            name = field.child_by_field_name("name")
            declared = self.type_of(field.child_by_field_name("type"))
            if name is not None and declared is not None:
                self.fields.setdefault(struct, {})[_text(name, self.source)] = declared

    def _use_paths(self, node: Node, prefix: list[str]) -> list[tuple[list[str], str | None, bool]]:
        """A use tree's leaves as (path segments, the name bound, glob?)."""
        kind = node.type
        if kind == "use_as_clause":
            segs = _segs(node.child_by_field_name("path"), self.source)
            alias = node.child_by_field_name("alias")
            if segs is None:
                return []
            return [(prefix + segs, _text(alias, self.source) if alias is not None else segs[-1], False)]
        if kind == "use_wildcard":
            inner = next((c for c in node.named_children), None)
            segs = _segs(inner, self.source) if inner is not None else []
            return [(prefix + (segs or []), None, True)]
        if kind in ("scoped_use_list", "use_list"):
            path = node.child_by_field_name("path")
            base = prefix + (_segs(path, self.source) or []) if path is not None else prefix
            items = node.child_by_field_name("list") if kind == "scoped_use_list" else node
            found = []
            for item in items.named_children if items is not None else []:
                if item.type == "self":
                    if base:
                        found.append((base, base[-1], False))
                else:
                    found += self._use_paths(item, base)
            return found
        segs = _segs(node, self.source)
        if segs is None:
            return []
        return [(prefix + segs, segs[-1], False)]

    # --- names to modules ---------------------------------------------------------

    def _lookup(self, table: dict[tuple, dict] | dict[tuple, set], name: str, scope: tuple):
        """`name` in `table` at `scope` or an enclosing scope of the file."""
        while True:
            entry = table.get(scope)
            if entry is not None and name in entry:
                return entry[name] if isinstance(entry, dict) else scope
            if len(scope) <= len(self.module):
                return None
            scope = scope[:-1]

    def _start(
        self, segs: list[str], scope: tuple, depth: int = 0, unbound: str | None = None,
    ) -> tuple[_Module, list[str]] | None:
        """The module a path's leading segments name and the rest of the
        path; None when it starts outside the repository (std, an external
        crate, `::x`) or with no module at all."""
        if not segs or depth > 8:
            return None
        first = segs[0]
        if first == "crate":
            return _Module(self.crate, ()), segs[1:]
        if first == "self":
            return _Module(self.crate, scope), segs[1:]
        if first == "super":
            path, i = scope, 0
            while i < len(segs) and segs[i] == "super":
                path, i = path[:-1], i + 1
            return _Module(self.crate, path), segs[i:]
        bound = self._lookup(self.bindings, first, scope) if first != unbound else None
        if bound is not None:
            # `use foo::x;` names a crate or module foo, not this binding again.
            return self._start(bound + segs[1:], self._lookup_scope(first, scope), depth + 1, bound[0])
        declared = self._lookup(self.declared, first, scope)
        if declared is not None:
            return _Module(self.crate, declared + (first,)), segs[1:]
        roots = self.crates.get(first)
        if roots:
            # Two packages of one name: the first here, `_also` adds the others.
            crate = _Crate(posixpath.dirname(roots[0]), (roots[0],), cross=True, shared=len(roots) > 1)
            return _Module(crate, ()), segs[1:]
        return None

    def _lookup_scope(self, name: str, scope: tuple) -> tuple:
        while scope not in self.bindings or name not in self.bindings[scope]:
            scope = scope[:-1]
        return scope

    def _also(self, crate: _Crate) -> list[_Crate]:
        """Every crate of the same name as the cross-crate `crate`."""
        if not crate.cross:
            return [crate]
        for roots in self.crates.values():
            if crate.root_files[0] in roots:
                return [_Crate(posixpath.dirname(root), (root,), cross=True, shared=len(roots) > 1) for root in roots]
        return [crate]

    def files(self, module: _Module) -> list[str]:
        """The files a module can be: this file for one of its inline
        modules, else `<root>/a/b.rs` and `<root>/a/b/mod.rs` (a crate root
        module: its root files)."""
        crate, path = module
        if not crate.cross and crate.root == self.crate.root and path in self.inline:
            return [self.file_path]
        found = []
        for each in self._also(crate):
            if path:
                base = _join(each.root, *path)
                candidates = [f"{base}.rs", f"{base}/mod.rs"]
            else:
                candidates = list(each.root_files)
            found += candidates
        return found

    def pins(self, module: _Module) -> list[str]:
        """A module's files, and the recursive prefix of its directory
        (package: an item re-exported from a submodule). The files of a
        crate whose name another package shares are package confidence:
        each a directory pin at package confidence limited to files ending
        with its name (`a/src/.~?lib.rs`, which a `mylib.rs` beside it
        would match too)."""
        found = self.files(module)
        if module.crate.shared:
            found = [f"{posixpath.dirname(each)}/.~?{posixpath.basename(each)}" for each in found]
        if module.path and found != [self.file_path]:
            found += [_join(each.root, *module.path) + "/" for each in self._also(module.crate)]
        return found

    def _walk(self, start: tuple[_Module, list[str]]) -> tuple[_Module, list[str]]:
        """Follow lowercase segments as modules down to the last one or a
        type: (the module, what is left: [item], [Type, item...] or [])."""
        module, rest = start
        while len(rest) > 1 and not _uppercase(rest[0]):
            module, rest = _Module(module.crate, module.path + (rest[0],)), rest[1:]
        return module, rest

    def path_pins(self, segs: list[str], scope: tuple) -> list[str] | None:
        """The pins of the function a path call names (`a::f`, `T::f`,
        `crate::m::T::f`), None when it is outside the repository."""
        if len(segs) == 2 and segs[0] in self.types:
            return [self.file_path]
        if len(segs) == 2 and segs[0] in STOP_TYPES["rust"] and self._lookup(self.bindings, segs[0], scope) is None:
            return None
        start = self._start(segs, scope)
        if start is None:
            unbound = self._lookup(self.bindings, segs[0], scope) is None
            return self._glob_pins(scope) if unbound and (len(segs) == 1 or len(segs) == 2 and _uppercase(segs[0])) \
                else None
        module, rest = self._walk(start)
        if not rest:
            return None
        return self.pins(module)

    def _glob_pins(self, scope: tuple) -> list[str] | None:
        found = []
        current = scope
        while True:
            for segs in self.globs.get(current, []):
                start = self._start(segs, current)
                if start is not None:
                    module, rest = start
                    found += self.pins(_Module(module.crate, module.path + tuple(rest)))
            if len(current) <= len(self.module):
                break
            current = current[:-1]
        return found or None

    def type_pins(self, declared: _Type, scope: tuple) -> list[str] | None:
        """The pins of a type's module: this file for a type declared here,
        None for a library type or one outside the repository."""
        segs = list(declared.segs)
        unbound = self._lookup(self.bindings, segs[0], scope) is None
        if len(segs) == 1:
            if segs[0] in self.types:
                return [self.file_path]
            if unbound and (segs[0] in STOP_TYPES["rust"] or segs[0] in _CONTAINERS or segs[0] == "[]"):
                return None
        start = self._start(segs, scope)
        if start is None:
            return self._glob_pins(scope) if len(segs) == 1 and unbound else None
        module, rest = start
        while len(rest) > 1:
            module, rest = _Module(module.crate, module.path + (rest[0],)), rest[1:]
        return self.pins(module) if rest else None

    # --- types ---------------------------------------------------------------------

    def type_of(self, node: Node | None, source: bytes | None = None) -> _Type | None:
        """A type expression as a `_Type`: references and smart pointers
        seen through, a container's or array's element kept for loops."""
        source = self.source if source is None else source
        if node is None:
            return None
        kind = node.type
        if kind in ("reference_type", "pointer_type"):
            return self.type_of(node.child_by_field_name("type"), source)
        if kind in ("array_type", "slice_type"):
            element = self.type_of(node.child_by_field_name("element"), source)
            return _Type(("[]",), element)
        if kind == "generic_type":
            base = _segs(node.child_by_field_name("type"), source)
            arguments = node.child_by_field_name("type_arguments")
            first = next(iter(arguments.named_children), None) if arguments is not None else None
            if base and base[-1] in _SMART_POINTERS:
                return self.type_of(first, source)
            if base and base[-1] in _CONTAINERS:
                return _Type(tuple(base), self.type_of(first, source))
            return _Type(tuple(base)) if base else None
        segs = _segs(node, source) if kind in ("type_identifier", "scoped_type_identifier") else None
        return _Type(tuple(segs)) if segs else None

    def _used_as(self, name: str) -> tuple[bool, bool]:
        """Whether the file's code (its `use`s left out) writes `name::`
        (a module) and `name(` (a function)."""
        word = re.escape(name)
        as_module = re.search(rf"(?<![\w:.]){word}\s*::", self._code) is not None
        as_call = re.search(rf"(?<![\w:.]){word}\s*\(", self._code) is not None
        return as_module, as_call

    def imports(self) -> tuple[set[str], set[str]]:
        """The Modules the file's `mod` declarations and `use`s name: files
        of its own crate and of other crates. A `use a::b::X`
        imports the module of X (a::b) when X is a type or used as a
        function, the module a::b::x when x is used as one (`x::f()`), else
        either."""
        modules: list[_Module] = [_Module(self.crate, scope + (name,)) for scope, name in self.mod_decls]
        for scope, segs, binding, glob in self.uses:
            start = self._start(segs, scope)
            if start is None:
                continue
            module, rest = start
            if glob:
                modules.append(_Module(module.crate, module.path + tuple(rest)))
                continue
            while len(rest) > 1 and not _uppercase(rest[0]):
                module, rest = _Module(module.crate, module.path + (rest[0],)), rest[1:]
            if not rest or rest[0] == "self":
                modules.append(module)
                continue
            if _uppercase(rest[0]):
                modules.append(module)
                continue
            as_module, as_call = self._used_as(binding or rest[0])
            if as_module or not as_call:
                modules.append(_Module(module.crate, module.path + (rest[0],)))
            if as_call or not as_module:
                modules.append(module)
        files: set[str] = set()
        pins: set[str] = set()
        for module in modules:
            for each in self.files(module):
                if each != self.file_path:
                    files.add(each)
        return files, pins


def _function_scope(node: Node, resolver: _RustResolver, self_type: _Type | None) -> dict[str, _Type | None]:
    """A function's parameters by name, with their types."""
    scope: dict[str, _Type | None] = {}
    params = node.child_by_field_name("parameters")
    for param in params.named_children if params is not None else []:
        if param.type == "self_parameter":
            scope["self"] = self_type
        elif param.type == "parameter":
            pattern = param.child_by_field_name("pattern")
            if pattern is not None and pattern.type == "identifier":
                scope[_text(pattern, resolver.source)] = resolver.type_of(param.child_by_field_name("type"))
    return scope


class _CallCollector:
    """One caller's call sites, resolved by the tiers (see the module
    docstring): (callee name, pins or None for a bare name, member call?)."""

    def __init__(self, resolver: _RustResolver, scope: tuple, self_type: _Type | None,
                 variables: dict[str, _Type | None]) -> None:
        self.resolver = resolver
        self.scope = scope
        self.self_type = self_type
        self.variables = variables
        self.found: list[tuple[str, list[str] | None, bool]] = []

    def expr_type(self, node: Node | None, source: bytes) -> _Type | None:
        if node is None:
            return None
        kind = node.type
        if kind in ("identifier", "self"):
            return self.variables.get(_text(node, source))
        if kind in ("reference_expression", "parenthesized_expression", "try_expression"):
            inner = node.child_by_field_name("value") or next(iter(node.named_children), None)
            return self.expr_type(inner, source) if kind != "try_expression" else None
        if kind == "field_expression":
            owner = self.expr_type(node.child_by_field_name("value"), source)
            field = node.child_by_field_name("field")
            if owner is not None and len(owner.segs) == 1 and field is not None:
                return self.resolver.fields.get(owner.segs[0], {}).get(_text(field, source))
            return None
        if kind == "struct_expression":
            segs = _segs(node.child_by_field_name("name"), source)
            return self._typed(segs)
        if kind == "call_expression":
            function = node.child_by_field_name("function")
            if function is not None and function.type == "field_expression":
                method = function.child_by_field_name("field")
                if method is not None and _text(method, source) in ("iter", "iter_mut", "into_iter"):
                    return self.expr_type(function.child_by_field_name("value"), source)
                return None
            segs = _segs(function, source)
            # `T::new()` and its like: a type's associated function makes a T.
            if segs and len(segs) >= 2 and _uppercase(segs[-2]):
                return self._typed(segs[:-1])
        return None

    def _typed(self, segs: list[str] | None) -> _Type | None:
        if not segs:
            return None
        if segs == ["Self"]:
            return self.self_type
        return _Type(tuple(segs))

    def bind(self, pattern: Node | None, declared: _Type | None) -> None:
        if pattern is not None and pattern.type == "identifier":
            self.variables[_text(pattern, self.resolver.source)] = declared
        elif pattern is not None and pattern.type == "mut_pattern":
            self.bind(next(iter(pattern.named_children), None), declared)

    def walk(self, node: Node, source: bytes) -> None:
        kind = node.type
        if kind in _SCOPE_BOUNDARY_TYPES:
            return
        if kind == "let_declaration" and source is self.resolver.source:
            value = node.child_by_field_name("value")
            declared = self.resolver.type_of(node.child_by_field_name("type"))
            if declared is None:
                declared = self.expr_type(value, source)
            self.bind(node.child_by_field_name("pattern"), declared)
        elif kind == "for_expression" and source is self.resolver.source:
            iterable = self.expr_type(node.child_by_field_name("value"), source)
            self.bind(node.child_by_field_name("pattern"), iterable.element if iterable is not None else None)
        elif kind == "closure_expression" and source is self.resolver.source:
            params = node.child_by_field_name("parameters")
            for param in params.named_children if params is not None else []:
                self.bind(param if param.type == "identifier" else param.child_by_field_name("pattern"), None)
        elif kind == "call_expression":
            self.call(node, source)
        elif kind == "macro_invocation":
            self.macro(node, source)
        for child in node.children:
            self.walk(child, source)

    def macro(self, node: Node, source: bytes) -> None:
        """The calls in a macro's arguments (`println!("{}", f(x))`): its
        token tree reparsed as a call's arguments."""
        tree = next((c for c in node.named_children if c.type == "token_tree"), None)
        if tree is None or not tree.children or tree.children[0].type not in ("(", "["):
            return
        inner = source[tree.children[0].end_byte: tree.children[-1].start_byte]
        if b"(" not in inner:
            return
        wrapped = b"fn __devgraph_macro__() { __devgraph_macro__(" + inner + b"); }"
        reparsed = _make_parser().parse(wrapped)
        body = reparsed.root_node.named_children[0].child_by_field_name("body") if reparsed.root_node.named_children \
            else None
        arguments = None
        if body is not None and body.named_children and body.named_children[0].named_children:
            outer = body.named_children[0].named_children[0]
            arguments = outer.child_by_field_name("arguments") if outer.type == "call_expression" else None
        for child in arguments.named_children if arguments is not None else []:
            self.walk(child, wrapped)

    def call(self, node: Node, source: bytes) -> None:
        function = node.child_by_field_name("function")
        if function is None:
            return
        if function.type == "generic_function":
            function = function.child_by_field_name("function")
            if function is None:
                return
        resolver = self.resolver
        if function.type == "identifier":
            name = _text(function, source)
            if name in self.variables or _uppercase(name):
                return  # a closure or function value, or a tuple struct / variant
            if name in resolver.functions:
                self.found.append((name, [resolver.file_path], False))
                return
            pins = resolver.path_pins([name], self.scope)
            if pins:
                self.found.append((name, pins, False))
            return
        if function.type == "scoped_identifier":
            segs = _segs(function, source)
            if not segs or segs[0] == "" or len(segs) < 2:
                return
            name = segs[-1]
            if segs[0] == "Self":
                pins = resolver.type_pins(self.self_type, self.scope) if self.self_type is not None else None
            else:
                pins = resolver.path_pins(segs, self.scope)
            if pins:
                self.found.append((name, pins, False))
            return
        if function.type == "field_expression":
            field = function.child_by_field_name("field")
            if field is None:
                return
            name = _text(field, source)
            receiver = function.child_by_field_name("value")
            typed = self.expr_type(receiver, source)
            if typed is not None:
                owner = typed.segs[-1]
                if len(typed.segs) == 1 and owner in resolver.types:
                    if name in resolver.methods.get(owner, set()):
                        self.found.append((name, [resolver.file_path], False))
                        return
                    # A trait's method, or one an impl in another file adds.
                else:
                    pins = resolver.type_pins(typed, self.scope)
                    if pins:
                        self.found.append((name, pins, False))
                    return
            if name not in STOP_METHODS["rust"]:
                self.found.append((name, None, True))


def extract_rust_file(
    source_code: str, file_path: str, repo_id: str, config: ResolverConfig | None = None,
) -> ExtractionResult:
    """Parse a Rust file and extract nodes and relationships.

    Args:
        source_code: The Rust source code as a string.
        file_path: The Module node's identity -- the file's path relative to
            the repo root, using forward slashes (e.g. 'src/lib.rs').
        repo_id: Repository ID for scoping nodes.
        config: The repository's resolver configuration (its Cargo.toml
            files), read once per batch: another crate's name names its
            library. None makes every other crate external.

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

    module_properties: dict = {"type": "module", "source_file": file_path}
    module_doc = _extract_module_doc(root, source_bytes)
    if module_doc:
        module_properties["description"] = _docstring_summary(module_doc)
        module_properties["docstring_full"] = module_doc
    module_node = GraphNode(label="Module", repo_id=repo_id, name=file_path, properties=module_properties)
    result.nodes.append(module_node)

    resolver = _RustResolver(root, source_bytes, file_path, config)
    import_files, import_pins = resolver.imports()
    result.relationships += import_rows(file_path, repo_id, import_files, import_pins)
    sites = CallSites()

    def _record_calls(
        body: Node, caller_label: str, caller_name: str, caller_class: str | None, scope: tuple,
        self_type: _Type | None, variables: dict,
    ) -> None:
        collector = _CallCollector(resolver, scope, self_type, variables)
        collector.walk(body, source_bytes)
        for name, pins, member in collector.found:
            sites.add(caller_label, caller_name, caller_class, name, pins, member)

    def visit_block(block: Node, parent_name: str | None, parent_label: str, scope: tuple) -> None:
        """Visit statements in a block (source_file, impl/trait body, fn
        body). Iterates raw `.children` (not `.named_children`) so doc
        comments stay positionally adjacent for `_leading_doc_comment`'s
        `prev_sibling` walk."""
        for node in block.children:
            if node.type == "struct_item":
                _visit_type_item(node, "struct", parent_name, parent_label)
            elif node.type == "enum_item":
                _visit_type_item(node, "enum", parent_name, parent_label)
            elif node.type == "trait_item":
                _visit_trait(node, parent_name, parent_label, scope)
            elif node.type == "impl_item":
                _visit_impl(node, parent_name, parent_label, scope)
            elif node.type == "function_item":
                _visit_function(node, parent_name, parent_label, scope)
            elif node.type == "mod_item":
                body_node = node.child_by_field_name("body")
                name_node = node.child_by_field_name("name")
                if body_node is not None and name_node is not None:
                    # Inline `mod foo { .. }` -- its items flattened into the
                    # same graph scope (see module docstring's known
                    # limitations), its paths resolved inside module foo.
                    visit_block(body_node, parent_name, parent_label, scope + (_text(name_node, source_bytes),))
            elif node.type in (
                "line_comment",
                "block_comment",
                "use_declaration",
                "visibility_modifier",
                "attribute_item",
                "inner_attribute_item",
                "{",
                "}",
            ):
                continue
            else:
                # Any other statement (const/static items, etc.) at this
                # scope -- attribute its call expressions to the enclosing
                # scope, same as Python's module/class-body-level handling.
                caller_class = parent_name if parent_label == "Class" else None
                _record_calls(node, parent_label, parent_name if parent_name else file_path, caller_class, scope,
                              None, {})

    def _visit_type_item(node: Node, kind: str, parent_name: str | None, parent_label: str) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        type_name = _text(name_node, source_bytes)

        properties: dict = {
            "type": "class",
            "kind": kind,
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _leading_doc_comment(node, source_bytes)
        if doc:
            properties["description"] = _docstring_summary(doc)
            properties["docstring_full"] = doc

        result.nodes.append(GraphNode(label="Class", repo_id=repo_id, name=type_name, properties=properties))
        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Class",
                to_name=type_name,
                repo_id=repo_id,
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
            )
        )

    def _visit_trait(node: Node, parent_name: str | None, parent_label: str, scope: tuple) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        trait_name = _text(name_node, source_bytes)

        properties: dict = {
            "type": "class",
            "kind": "trait",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _leading_doc_comment(node, source_bytes)
        if doc:
            properties["description"] = _docstring_summary(doc)
            properties["docstring_full"] = doc

        result.nodes.append(GraphNode(label="Class", repo_id=repo_id, name=trait_name, properties=properties))
        result.relationships.append(
            GraphRelationship(
                from_label=parent_label,
                from_name=parent_name if parent_name else file_path,
                rel_type="CONTAINS",
                to_label="Class",
                to_name=trait_name,
                repo_id=repo_id,
                from_file=file_path if parent_label == "Class" else None,
                to_file=file_path,
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            # Default methods only (function_item has a body);
            # signature-only methods (function_signature_item) are skipped.
            for child in body_node.children:
                if child.type == "function_item":
                    _visit_function(child, trait_name, "Class", scope)

    def _visit_impl(node: Node, parent_name: str | None, parent_label: str, scope: tuple) -> None:
        type_field = node.child_by_field_name("type")
        target_name = _type_name(type_field, source_bytes)
        if target_name is None:
            return

        trait_field = node.child_by_field_name("trait")
        if trait_field is not None:
            trait_name = _type_name(trait_field, source_bytes)
            if trait_name:
                result.relationships.append(
                    GraphRelationship(
                        from_label="Class",
                        from_name=target_name,
                        rel_type="EXTENDS",
                        to_label="Class",
                        to_name=trait_name,
                        repo_id=repo_id,
                    )
                )

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            for child in body_node.children:
                if child.type == "function_item":
                    _visit_function(child, target_name, "Class", scope)

    def _visit_function(node: Node, parent_name: str | None, parent_label: str, scope: tuple) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        properties: dict = {
            "type": "function",
            "file": file_path,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        doc = _leading_doc_comment(node, source_bytes)
        if doc:
            properties["description"] = _docstring_summary(doc)
            properties["docstring_full"] = doc

        result.nodes.append(GraphNode(label="Function", repo_id=repo_id, name=func_name, properties=properties))
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

        body_node = node.child_by_field_name("body")
        if body_node is not None:
            caller_class = parent_name if parent_label == "Class" else None
            self_type = _Type((parent_name,)) if parent_label == "Class" and parent_name else None
            _record_calls(body_node, "Function", func_name, caller_class, scope, self_type,
                          _function_scope(node, resolver, self_type))

            # Nested fn items (rare, but Tree-sitter walks these fine).
            for child in body_node.children:
                if child.type == "function_item":
                    _visit_function(child, func_name, "Function", scope)

    visit_block(root, None, "Module", resolver.module)
    result.relationships += sites.rows(file_path, repo_id)

    return own_edges(result, file_path)


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a Rust file and upsert results into the graph.

    Thin wrapper calling extract_rust_file() and upserting each node and
    relationship via the GraphEngine, matching the Python extractor's
    index_file() wrapper.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Rust file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes) -- what makes `mod`/`use` resolution and same-named
            files in different directories work correctly. When omitted,
            falls back to the bare filename.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    source_code = file_path.read_text(encoding="utf-8")

    config = None
    if repo_root is not None:
        try:
            rel = file_path.resolve().relative_to(Path(repo_root).resolve())
            module_name = rel.as_posix()
        except ValueError:
            module_name = file_path.name
        config = ResolverConfig(Path(repo_root))
    else:
        module_name = file_path.name

    result = extract_rust_file(source_code, module_name, repo_id, config)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
