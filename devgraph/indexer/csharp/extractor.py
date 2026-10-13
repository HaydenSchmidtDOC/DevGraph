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
  - Match keys (`mkeys`) on every Class and Function: a class's
    `Ns:Name` (`.` the global namespace, `Ns:Outer.Inner` nested), a
    method's `Ns:Owner`, an extension method's also `Ns:+T` (T the simple
    name of its `this` parameter's type). `Ns` is the file's own text's
    namespace (block namespaces joined, or the file-scoped one); same-named
    declarations of one file share their keys' union.
  - CALLS and IMPORTS resolved through types and namespaces (see
    docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md,
    C#). Nothing ties a namespace to a folder, nor a class to a file named
    after it, so another file's type `T` is any C# file (`/.?.cs^`, no
    vendored copy) whose node holds one of the keys the site can mean
    (`@`, graph/schema.py `scoped_pin`): `Ns:T` for each namespace a name
    written there reaches (the enclosing namespaces, innermost first, and
    the global one; the usings of its namespace blocks, also relative to
    each enclosing namespace, and of the file; its project's `<Using>`
    items and `global using`s, read from the resolver configuration;
    `System` and `Microsoft` ones left out unless the site's own namespace
    is under the same root), an alias's target (its
    block's), a dotted name at every split (`A.B.T`: `A.B:T`, `A:B.T`,
    `.:A.B.T`, as a type can be nested; a qualified one also under each
    enclosing namespace, and as a type nested in `A.B`), a nested
    `Ns:Outer.T` inside Outer. The keys of one type lookup come in the
    tiers C# looks the name up in, nearest first (graph/schema.py
    `tiered_pin`): a nearer type hides a farther one's (an enclosing
    namespace's `User` a using's). A site's types' keys and its extension keys
    (`Ns:+T`) are two pins, each capped at 32 keys on its own; a group over
    the cap falls back to the class file pins written before match keys
    (`/T.cs^`, `/TExtensions.cs^`). Package confidence either way: two
    projects can each hold `Ns.T`.
    CALLS, by the first tier that applies: an unqualified `M()`, `this.M()`
    or `base.M()` -> this file when an enclosing class declared here (or an
    in-file base) has M, else its other bases' keys, a partial class's own
    keys (another part has it) and the `using static` types' keys; a
    static `T.M()` (an alias's target type, a qualified `A.B.T.M()`) and a
    method on a typed receiver (a parameter, a field or property of the
    enclosing classes, `T x`, `var x = new T()`, `new T().M()`, a `foreach`
    element of a `List<T>` or array) -> this file when T is declared here
    and has M, else T's keys, and for a receiver also the extension methods
    `Ns:+T` of the site's namespaces; a library type (calls.STOP_TYPES) or
    a predefined one links nothing; anything else, `dynamic` and `x?.M()`
    included, a bare-name edge, which a member call never ends at its
    caller and a stoplisted method (calls.STOP_METHODS["csharp"]) never
    gets.
    IMPORTS: an edge to each class the file writes as a type (a base, a
    member, parameter, local or return type, `new T()`, a cast, a generic
    argument, a static receiver, an alias's or used `using static` target)
    under its keys, not declared in this file and no library type; and to
    each method `M` a typed receiver's `x.M()` can reach as an extension
    method (`Ns:+T`), unless this file declares an `M`: the file depends on
    an extension class it never names. A `using` of a namespace imports
    nothing.
  - Project visibility: every Class and Function carries its project's
    directory as `proj` ("." the repository root's, "" for a file of no
    project), and every path-pinned CALLS and IMPORTS row of a file in a
    project its `visible` projects (its own and those its
    `<ProjectReference>`s reach, transitively): such a row links only
    nodes of those projects or of none (common.visible_to).

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
  - `ImplicitUsings` is not read (only System.* and Microsoft.*
    namespaces), nor is a `<ProjectReference>`'s `Condition` or `$(...)`
    evaluated (resolver_config).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Node, Parser

from devgraph.graph.schema import MAX_SCOPE_KEYS, pin_kind, scoped_pin, tiered_pin
from devgraph.indexer.apis.routes import add_route, join_route
from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, CallSites, call_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship, own_edges, owned_pin
from devgraph.indexer.resolver_config import CsUsing, ResolverConfig

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


# --- resolution ------------------------------------------------------------------

#: The pin of another file's type: a class declared in any C# file, vendored
#: copies left out, narrowed by match keys (`scoped_pin`).
_ANY_CS_FILE = "/.?.cs^"
#: The global namespace, as a match key writes it.
_GLOBAL_NS = "."
#: Type names that name no class (`var`, `dynamic`).
_NOT_TYPES = frozenset({"var", "dynamic", "nint", "nuint"})


class _CsType(NamedTuple):
    """A declared type: its simple name, the qualifier it was written with
    (`Shop.Domain` for `Shop.Domain.User`), and the element type a
    `foreach` over it yields (`List<User>`: `User`)."""

    name: str
    element: _CsType | None = None
    qualifier: str = ""


def _dotted(node: Node | None, source: bytes) -> str | None:
    """A name or member access written as a dotted name (`A.B.C`), type
    arguments and an extern alias left off, `global::` kept; None for
    anything else."""
    if node is None:
        return None
    kind = node.type
    if kind == "identifier":
        return _text(node, source)
    if kind == "generic_name":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        return _text(ident, source) if ident is not None else None
    if kind == "alias_qualified_name":
        alias = node.child_by_field_name("alias")
        name = _dotted(node.child_by_field_name("name"), source)
        return f"global::{name}" if name is not None and alias is not None and _text(alias, source) == "global" else name
    if kind in ("qualified_name", "member_access_expression"):
        head = _dotted(node.child_by_field_name("qualifier") or node.child_by_field_name("expression"), source)
        tail = _dotted(node.child_by_field_name("name"), source)
        return f"{head}.{tail}" if head is not None and tail is not None else None
    return None


def _splits(dotted: str) -> set[str]:
    """Every match key the type `dotted` (`A.B.T`) can have, one per split
    into a namespace and a type path, as a type can be nested in another:
    `A.B:T`, `A:B.T` and `.:A.B.T`."""
    parts = dotted.split(".")
    return {f"{'.'.join(parts[:i]) or _GLOBAL_NS}:{'.'.join(parts[i:])}" for i in range(len(parts))}


def _global(node: Node, source: bytes) -> str:
    """`global::` for a name written `global::T`, else nothing."""
    alias = node.child_by_field_name("alias")
    return "global::" if alias is not None and _text(alias, source) == "global" else ""


def _type_refs(node: Node | None, source: bytes, qualifier: str = "") -> list[tuple[str, str]]:
    """Every type a type expression mentions, as (the qualifier it was
    written with, its simple name) (`Dictionary<Key, A.Item>`: Dictionary,
    Key, (A, Item))."""
    if node is None:
        return []
    kind = node.type
    if kind == "identifier":
        return [(qualifier, _text(node, source))]
    if kind == "qualified_name":
        return _type_refs(node.child_by_field_name("name"), source,
                          _dotted(node.child_by_field_name("qualifier"), source) or "")
    if kind == "alias_qualified_name":
        return _type_refs(node.child_by_field_name("name"), source, _global(node, source) or qualifier)
    if kind == "generic_name":
        found = []
        for child in node.named_children:
            if child.type == "identifier":
                found.append((qualifier, _text(child, source)))
            elif child.type == "type_argument_list":
                for argument in child.named_children:
                    found += _type_refs(argument, source)
        return found
    if kind in ("nullable_type", "array_type", "pointer_type", "ref_type", "scoped_type", "tuple_type",
                "tuple_element"):
        found = []
        for child in node.named_children:
            found += _type_refs(child, source)
        return found
    return []


def _type_names(node: Node | None, source: bytes) -> list[str]:
    """Every simple type name a type expression mentions (`Dictionary<Key,
    List<Item>>`: Dictionary, Key, List, Item; `Shop.Domain.User`: User)."""
    return [name for _qualifier, name in _type_refs(node, source)]


def _declared_type(node: Node | None, source: bytes, qualifier: str = "") -> _CsType | None:
    """A variable's declared type as the class its members are on (`User?`:
    User; an array or a predefined type: none), with a generic's last type
    argument as the element a `foreach` yields."""
    if node is None:
        return None
    kind = node.type
    if kind == "identifier":
        return _CsType(_text(node, source), None, qualifier) if _text(node, source) not in _NOT_TYPES else None
    if kind == "qualified_name":
        return _declared_type(node.child_by_field_name("name"), source,
                              _dotted(node.child_by_field_name("qualifier"), source) or "")
    if kind == "alias_qualified_name":
        return _declared_type(node.child_by_field_name("name"), source, _global(node, source) or qualifier)
    if kind == "nullable_type":
        return _declared_type(node.child_by_field_name("type") or next(iter(node.named_children), None), source)
    if kind == "generic_name":
        name = next((c for c in node.named_children if c.type == "identifier"), None)
        arguments = next((c for c in node.named_children if c.type == "type_argument_list"), None)
        last = arguments.named_children[-1] if arguments is not None and arguments.named_children else None
        return _CsType(_text(name, source), _declared_type(last, source), qualifier) if name is not None else None
    if kind == "array_type":
        return _CsType("[]", _declared_type(node.child_by_field_name("type"), source))
    return None


def _using(node: Node, source: bytes) -> CsUsing | None:
    """A using directive as the configuration's `CsUsing` writes one: its
    namespace or type (dotted, `global::` kept: the scope reads it as
    absolute), `using static` and an alias."""
    alias = node.child_by_field_name("name")
    target = next(
        (c for c in node.named_children
         if c.type in ("identifier", "qualified_name", "generic_name", "alias_qualified_name")
         and (alias is None or (c.start_byte, c.end_byte) != (alias.start_byte, alias.end_byte))),
        None,
    )
    name = _dotted(target, source)
    if name is None:
        return None
    return CsUsing(name, any(c.type == "static" for c in node.children), _text(alias, source) if alias else "")


class _CsScope(NamedTuple):
    """What a site's type names can mean: `chain` the namespaces enclosing
    it, innermost first (`A.B`, `A`); `namespaces` every namespace whose
    types it names unqualified (the chain, the global namespace and the
    usings in scope: the namespace blocks', the file's and its project's,
    `System` and `Microsoft` ones left out); `aliases` each using alias's
    target, and `statics` each `using static` type, as the dotted names it
    can be (a namespace block's using is relative to each enclosing
    namespace too); `levels` the order C# looks a name up in: each
    enclosing namespace, innermost first, then the global one (`.`), with
    the namespaces the usings of its declaration bring in (the global
    one's: the file's and its project's); `alias_levels` the level each
    alias is declared at (the innermost declaration's)."""

    chain: tuple[str, ...]
    namespaces: frozenset[str]
    aliases: dict[str, tuple[str, ...]]
    statics: tuple[tuple[str, ...], ...]
    levels: tuple[tuple[str, frozenset[str]], ...]
    alias_levels: dict[str, str]

    @property
    def namespace(self) -> str:
        return self.chain[0] if self.chain else _GLOBAL_NS


class _CsClass:
    def __init__(self) -> None:
        self.methods: set[str] = set()
        self.fields: dict[str, _CsType | None] = {}
        # (name as written, the class it names or None for none or a library type, its match key tiers)
        self.bases: list[tuple[str, str | None, tuple[frozenset[str], ...]]] = []
        self.keys: set[str] = set()  # its own match keys, `Ns:Outer.Name`
        self.partial = False
        # What its members are owned by (common.member_owner), `Outer.Name`:
        # one per class of the name in the file.
        self.owners: set[str] = set()

    def members_owner(self) -> str | None:
        """The owner of the class's members, None when several classes of
        the file share its name."""
        return next(iter(self.owners)) if len(self.owners) == 1 else None


def _clean(keys) -> set[str]:
    """Match keys as a pin writes them: a verbatim identifier's `@` left off."""
    return {key.replace("@", "") for key in keys}


def _key_pin(tiers: list[set[str]]) -> str:
    """The pin to any C# file whose node holds one of these tiers' keys
    (graph/schema.py `tiered_pin`; with more than MAX_SCOPE_KEYS, any C#
    file)."""
    return tiered_pin(_ANY_CS_FILE, [_clean(tier) for tier in tiers])


def _key_group(tiers: list[set[str]], fallback) -> list[str]:
    """One group of a site's match keys, in tiers, as pins: one pin to any
    C# file whose node holds one of them (`_key_pin`), or, with more than
    MAX_SCOPE_KEYS, the `fallback` filename pins (`/T.cs^`, as before match
    keys), which have no tiers."""
    keys = _clean(set().union(*tiers))
    if not keys:
        return []
    if len(keys) <= MAX_SCOPE_KEYS:
        return [_key_pin(tiers)]
    return sorted({pin.replace("@", "") for pin in fallback})


def _merged(tiers: list[set[str]], other: list[set[str]]) -> list[set[str]]:
    """Two lookups' key tiers as one group: either alone keeps its tiers;
    both are one tier, so that one type's nearer candidate never hides
    another type's."""
    if not any(tiers) or not any(other):
        return [set(tier) for tier in (tiers if any(tiers) else other)]
    return [set().union(*tiers, *other)]


class _Target:
    """Where a call site's callee can be: files, and two groups of match
    keys in tiers (`_key_group`), each capped on its own: the types'
    (`Ns:T`, falling back to each type's class file `/T.cs^`) and their
    extension methods' (`Ns:+T`, falling back to `/TExtensions.cs^`)."""

    def __init__(self) -> None:
        self.files: set[str] = set()
        self.keys: list[set[str]] = []
        self.types: set[str] = set()  # the types `keys` are of
        self.extension_keys: list[set[str]] = []
        self.extended: set[str] = set()  # the types `extension_keys` are of

    def __bool__(self) -> bool:
        return bool(self.files or any(self.keys) or any(self.extension_keys))

    def add(self, other: _Target) -> None:
        self.files |= other.files
        self.keys = _merged(self.keys, other.keys)
        self.types |= other.types
        self.extension_keys = _merged(self.extension_keys, other.extension_keys)
        self.extended |= other.extended

    def pins(self) -> list[str]:
        return (sorted(self.files) + _key_group(self.keys, (f"/{name}.cs^" for name in self.types))
                + _key_group(self.extension_keys, (f"/{name}Extensions.cs^" for name in self.extended)))


def _library_namespace(name: str) -> bool:
    """`System`, `Microsoft` or a namespace under one: the framework's,
    unless the site's own namespace is under the same root (the framework's
    own repositories, `Microsoft.AspNetCore.Http` using
    `Microsoft.AspNetCore.Routing`)."""
    return name.split(".", 1)[0] in ("System", "Microsoft")


def _leftmost(qualifier: str) -> str:
    return qualifier.removeprefix("global::").split(".", 1)[0]


class _CsResolver:
    """What one C# file's names refer to: its classes (members, fields and
    bases, partial parts merged), the type parameters it declares, and each
    site's namespaces and aliases (`scope`)."""

    def __init__(self, root: Node, source: bytes, file_path: str, project_usings: tuple[CsUsing, ...] = ()) -> None:
        self.source = source
        self.file_path = file_path
        self.classes: dict[str, _CsClass] = {}
        self.types: set[str] = set()
        self.type_params: set[str] = set()
        self.imports: dict[str, set[str]] = {}  # class name -> IMPORTS pins
        self.extension_imports: dict[str, set[str]] = {}  # method name -> IMPORTS pins
        self._scopes: dict[int | None, _CsScope] = {}
        file_ns = next((c for c in root.named_children if c.type == "file_scoped_namespace_declaration"), None)
        self._file_namespace = _dotted(file_ns.child_by_field_name("name"), source) if file_ns is not None else None
        # The file's usings before a file-scoped namespace are absolute, those after it inside it.
        self._unit_usings = list(project_usings)
        self._file_ns_usings = []
        for child in root.named_children:
            if child.type == "using_directive" and (using := _using(child, source)) is not None:
                inside = file_ns is not None and child.start_byte > file_ns.start_byte
                (self._file_ns_usings if inside else self._unit_usings).append(using)
        for each in _descendants(root, "type_parameter"):
            name = each.child_by_field_name("name")
            if name is not None:
                self.type_params.add(_text(name, source))
        self._collect(root, ())
        self.methods: set[str] = set().union(*(info.methods for info in self.classes.values()))  # declared here

    def scope(self, node: Node) -> _CsScope:
        """The namespaces at `node` (see `_CsScope`)."""
        blocks = []
        current = node
        while current is not None:
            if current.type == "namespace_declaration":
                blocks.append(current)
            current = current.parent
        cache = blocks[0].start_byte if blocks else None
        if cache in self._scopes:
            return self._scopes[cache]

        def prefixes(parts: list[str]) -> list[str]:
            return [".".join(parts[:i]) for i in range(len(parts), 0, -1)]

        def relative(using: CsUsing, parts: list[str]) -> tuple[CsUsing, tuple[str, ...]]:
            if using.name.startswith("global::"):
                return absolute(using)
            return using, (using.name, *(f"{prefix}.{using.name}" for prefix in prefixes(parts)))

        def absolute(using: CsUsing) -> tuple[CsUsing, tuple[str, ...]]:
            return using, (using.name.removeprefix("global::"),)

        parts = [part for part in (self._file_namespace or "").split(".") if part]
        # Each using with the namespace whose declaration holds it (the
        # global one's: the file's and its project's).
        usings = [(_GLOBAL_NS, *absolute(using)) for using in self._unit_usings]
        usings += [(".".join(parts), *relative(using, parts)) for using in self._file_ns_usings]
        for block in reversed(blocks):
            parts += [part for part in (_dotted(block.child_by_field_name("name"), self.source) or "").split(".")
                      if part]
            body = block.child_by_field_name("body")
            usings += [(".".join(parts), *relative(u, parts)) for c in (body.named_children if body is not None else [])
                       if c.type == "using_directive" if (u := _using(c, self.source)) is not None]
        chain = tuple(prefixes(parts))
        root = parts[0] if parts else None  # a library root the file's own namespace is under is the repo's
        used = {
            level: frozenset(
                name for at, using, names in usings if at == level and not using.static and not using.alias
                for name in names if not _library_namespace(name) or name.split(".", 1)[0] == root
            )
            for level in (*chain, _GLOBAL_NS)
        }
        found = _CsScope(
            chain,
            frozenset({*chain, _GLOBAL_NS, *(name for names in used.values() for name in names)}),
            {using.alias: names for _at, using, names in usings if using.alias},
            tuple(sorted({names for _at, using, names in usings if using.static and not using.alias})),
            tuple(used.items()),
            {using.alias: at for at, using, _names in usings if using.alias},
        )
        self._scopes[cache] = found
        return found

    def type_keys(self, qualifier: str, name: str, scope: _CsScope, classes: list[str]) -> list[set[str]]:
        """The match keys the type `qualifier.name`, written at a site in
        `scope` inside `classes` (innermost first), can have, in the tiers C#
        looks it up in, nearest first (graph/schema.py `tiered_pin`):
        unqualified, the name nested in each enclosing class
        (`Ns:Outer.Inner`), innermost first, then for each level of
        `scope.levels` the name in its namespace and then in its usings'
        namespaces; qualified `A.B.T` the same way, each split within it
        relative to the level's namespace and `A.B.T` nested in a type `A`
        of it, then `A.B.T` nested in a type of its usings' namespaces (at
        the global level, each split of it: `global::` only there). A using
        alias for the name (or for `A`) is its declaration level's: after
        that level's namespace, in place of its usings, its target split at
        each dot (`_splits`: a type can be nested); the lookup ends there."""
        if qualifier.startswith("global::"):
            return [_splits(".".join(part for part in (qualifier.removeprefix("global::"), name) if part))]
        head, _dot, rest = qualifier.partition(".")
        alias = head if qualifier else name
        aliased = set().union(*(_splits(".".join(part for part in (target, rest, name if qualifier else "") if part))
                                for target in scope.aliases.get(alias, ())))
        dotted = f"{qualifier}.{name}" if qualifier else name
        tiers = []
        if not qualifier:
            path = list(reversed(classes))
            tiers = [{f"{scope.namespace}:{'.'.join([*path[:i], name])}"} for i in range(len(path), 0, -1)]
        for namespace, usings in scope.levels:
            if not qualifier:
                tiers.append({f"{namespace}:{name}"})
            else:
                tiers.append(_splits(dotted) if namespace == _GLOBAL_NS else {f"{namespace}:{dotted}"} | {
                    f"{namespace}.{key}" for key in _splits(dotted) if not key.startswith(_GLOBAL_NS + ":")
                })
            if aliased and scope.alias_levels.get(alias) == namespace:
                tiers.append(aliased)
                return tiers
            tiers.append({f"{using}:{dotted}" for using in usings})
        return tiers

    @staticmethod
    def extension_keys(name: str, scope: _CsScope) -> list[set[str]]:
        """The keys of the extension methods of type `name` a site in
        `scope` reaches (`Ns:+T`), a tier per level of `scope.levels`, its
        namespace and its usings' together: C# looks for extension methods
        one enclosing namespace declaration at a time."""
        return [_clean({f"{namespace}:+{name}"} | {f"{using}:+{name}" for using in usings})
                for namespace, usings in scope.levels]

    def _collect(self, node: Node, path: tuple[str, ...]) -> None:
        source = self.source
        for child in node.named_children:
            kind = child.type
            if kind in _CLASS_LIKE + ("interface_declaration", "enum_declaration"):
                name = child.child_by_field_name("name")
                if name is None:
                    continue
                cls = self.classes.setdefault(_text(name, source), _CsClass())
                self.types.add(_text(name, source))
                scope = self.scope(child)
                cls.keys |= _clean({f"{scope.namespace}:{'.'.join([*path, _text(name, source)])}"})
                cls.owners.add(".".join([*path, _text(name, source)]))
                cls.partial |= any(c.type == "modifier" and _text(c, source) == "partial" for c in child.children)
                base_list = next((c for c in child.named_children if c.type == "base_list"), None)
                for base in base_list.named_children if base_list is not None else []:
                    refs = _type_refs(base, source)
                    if refs:
                        qualifier, base_name = refs[0]
                        resolved = base_name if qualifier else self.type_name(base_name, scope)
                        if resolved in STOP_TYPES["csharp"] or _leftmost(qualifier) in STOP_TYPES["csharp"]:
                            resolved = None
                        keys = self.type_keys(qualifier, base_name, scope, list(reversed(path))) if resolved else []
                        cls.bases.append((base_name, resolved, tuple(frozenset(tier) for tier in keys)))
                body = child.child_by_field_name("body")
                for member in body.named_children if body is not None else []:
                    if member.type in _METHOD_LIKE:
                        member_name = member.child_by_field_name("name")
                        if member_name is not None:
                            cls.methods.add(_text(member_name, source))
                    elif member.type == "field_declaration":
                        declaration = next((c for c in member.named_children if c.type == "variable_declaration"), None)
                        declared = _declared_type(declaration.child_by_field_name("type"), source) \
                            if declaration is not None else None
                        for declarator in declaration.named_children if declaration is not None else []:
                            if declarator.type == "variable_declarator":
                                field = declarator.child_by_field_name("name")
                                if field is not None:
                                    cls.fields[_text(field, source)] = declared
                    elif member.type == "property_declaration":
                        prop = member.child_by_field_name("name")
                        if prop is not None:
                            cls.fields[_text(prop, source)] = _declared_type(member.child_by_field_name("type"), source)
                if body is not None:
                    self._collect(body, (*path, _text(name, source)))
            elif kind in ("namespace_declaration",):
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect(body, path)

    def type_name(self, name: str, scope: _CsScope) -> str | None:
        """A name written unqualified as a type at a site in `scope`, as the
        class it names: an alias's target's simple name; None for a type
        parameter or no class at all."""
        if name in scope.aliases:
            name = scope.aliases[name][0].rpartition(".")[2]
        if name in self.type_params or name in _NOT_TYPES:
            return None
        return name

    def type_target(
        self, written: _CsType, method: str, scope: _CsScope, classes: list[str], instance: bool,
    ) -> _Target | None:
        """Where `method` of the type `written` is: a class declared here,
        as `class_target` finds it (this file for another type declared
        here); another file's type by its keys (`type_keys`), which the file
        then imports, and on an instance also the extension methods of it
        the site's namespaces hold (`Ns:+T`), which it imports too; None for
        a library type."""
        resolved = written.name if written.qualifier else self.type_name(written.name, scope)
        if resolved is None or resolved == "[]" or resolved in STOP_TYPES["csharp"]:
            return None
        if _leftmost(written.qualifier) in STOP_TYPES["csharp"]:
            return None  # a library type's member (`DateTime.UtcNow`)
        target = _Target()
        if not written.qualifier and resolved in self.types:
            if resolved in self.classes:
                target = self.class_target(resolved, method)
            if not target and not instance:
                target.files.add(self.file_path)
        else:
            target.keys = self.type_keys(written.qualifier, written.name, scope, classes)
            target.types.add(resolved)
            self.imports.setdefault(resolved, set()).add(_key_pin(target.keys))
        if instance:
            extensions = self.extension_keys(resolved, scope)
            target.extension_keys = _merged(target.extension_keys, extensions)
            target.extended.add(resolved)
            if method not in self.methods and len(set().union(*extensions)) <= MAX_SCOPE_KEYS:
                self.extension_imports.setdefault(method, set()).add(_key_pin(extensions))
        return target

    def class_target(self, cls: str, method: str, seen: frozenset = frozenset(), own: bool = True) -> _Target:
        """Where `method` of class `cls` (declared here) is: this file when
        the class (`own`) or an in-file base declares it, as that class's
        member (common.owned_pin); else its other
        bases' keys, and for a partial class its own keys (another part, in
        another file, can declare it)."""
        target = _Target()
        info = self.classes.get(cls)
        if info is None or cls in seen:
            return target
        if own and method in info.methods:
            target.files.add(owned_pin(self.file_path, info.members_owner()))
            return target
        if own and info.partial:
            target.keys = _merged(target.keys, [info.keys])
            target.types.add(cls)
        for written, resolved, base_keys in info.bases:
            if written in self.classes:
                target.add(self.class_target(written, method, seen | {cls}))
            elif base_keys:
                target.keys = _merged(target.keys, list(base_keys))
                target.types.add(resolved)
        return target


def _descendants(node: Node, kind: str):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            yield current
        stack.extend(current.named_children)


def _enclosing_classes(node: Node, source: bytes) -> list[str]:
    """The names of the types enclosing `node`, innermost first."""
    found = []
    current = node.parent
    while current is not None:
        if current.type in _CLASS_LIKE + ("interface_declaration",):
            name = current.child_by_field_name("name")
            if name is not None:
                found.append(_text(name, source))
        current = current.parent
    return found


class _CsCalls:
    """One caller's call sites, resolved by the tiers (see the module
    docstring): (callee name, pins or None for a bare name, member call?)."""

    def __init__(
        self, resolver: _CsResolver, classes: list[str], variables: dict[str, _CsType | None], scope: _CsScope,
    ) -> None:
        self.resolver = resolver
        self.classes = classes  # the enclosing classes, innermost first
        self.variables = variables
        self.scope = scope
        self.locals: set[str] = set()  # local function names
        self.found: list[tuple[str, list[str] | None, bool]] = []

    def _field(self, name: str) -> tuple[bool, _CsType | None]:
        for cls in self.classes:
            info = self.resolver.classes.get(cls)
            if info is not None and name in info.fields:
                return True, info.fields[name]
        return False, None

    def walk(self, node: Node) -> None:
        source = self.resolver.source
        kind = node.type
        if kind in _NESTED_SCOPE_TYPES and kind != "local_function_statement":
            return
        if kind == "local_function_statement":
            name = node.child_by_field_name("name")
            if name is not None:
                self.locals.add(_text(name, source))
            return
        if kind == "variable_declaration":
            declared_node = node.child_by_field_name("type")
            declared = _declared_type(declared_node, source) if declared_node is not None else None
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = declarator.child_by_field_name("name")
                value = next((c for c in declarator.named_children if c != name), None)
                typed = declared
                if declared_node is not None and declared_node.type == "implicit_type":
                    typed = _declared_type(value.child_by_field_name("type"), source) \
                        if value is not None and value.type == "object_creation_expression" else None
                if name is not None:
                    self.variables[_text(name, source)] = typed
        elif kind == "foreach_statement":
            left = node.child_by_field_name("left")
            declared_node = node.child_by_field_name("type")
            typed = None
            if declared_node is not None and declared_node.type != "implicit_type":
                typed = _declared_type(declared_node, source)
            else:
                iterable = self.expr_type(node.child_by_field_name("right"))
                typed = iterable.element if iterable is not None else None
            if left is not None and left.type == "identifier":
                self.variables[_text(left, source)] = typed
        elif kind in ("lambda_expression", "anonymous_method_expression"):
            for param in _descendants(node, "parameter"):
                name = param.child_by_field_name("name")
                if name is not None:
                    self.variables[_text(name, source)] = _declared_type(param.child_by_field_name("type"), source)
            for param in node.named_children:
                if param.type == "identifier":
                    self.variables[_text(param, source)] = None
        elif kind == "invocation_expression":
            self.call(node)
        for child in node.children:
            self.walk(child)

    def expr_type(self, node: Node | None) -> _CsType | None:
        """The type of a receiver expression, None when nothing types it."""
        if node is None:
            return None
        source = self.resolver.source
        if node.type == "identifier":
            name = _text(node, source)
            if name in self.variables:
                return self.variables[name]
            return self._field(name)[1]
        if node.type == "object_creation_expression":
            return _declared_type(node.child_by_field_name("type"), source)
        if node.type == "member_access_expression":
            owner = node.child_by_field_name("expression")
            member = node.child_by_field_name("name")
            if owner is not None and owner.type == "this" and member is not None:
                return self._field(_text(member, source))[1]
        if node.type == "parenthesized_expression":
            return self.expr_type(next(iter(node.named_children), None))
        return None

    def _bare(self, name: str, member: bool) -> None:
        if not (member and name in STOP_METHODS["csharp"]):
            self.found.append((name, None, member))

    def _own(self, name: str, start_at_bases: bool = False) -> _Target:
        """`name` called unqualified, or on `this`/`base`: the enclosing
        classes and their bases, innermost first, up to the first that has
        it in this file (a `base.` call starts at the bases)."""
        target = _Target()
        for cls in self.classes:
            target.add(self.resolver.class_target(cls, name, own=not start_at_bases))
            if target.files or start_at_bases:
                break
        return target

    def _typed(self, written: _CsType, name: str, instance: bool) -> None:
        """A call of `name` on the type `written` (static) or an instance of it."""
        target = self.resolver.type_target(written, name, self.scope, self.classes, instance)
        if target is None:
            return
        if target:
            self.found.append((name, target.pins(), False))
        else:
            self._bare(name, True)

    def call(self, node: Node) -> None:
        source = self.resolver.source
        resolver = self.resolver
        function = node.child_by_field_name("function")
        if function is None:
            return
        if function.type == "generic_name":
            function = next((c for c in function.named_children if c.type == "identifier"), None)
            if function is None:
                return
        if function.type == "identifier":
            name = _text(function, source)
            if name in self.locals or name in self.variables:
                return  # a local function or a delegate
            target = self._own(name)
            if not target.files:
                for static in self.scope.statics:
                    simple = static[0].rpartition(".")[2]
                    if simple in resolver.types:
                        target.files.add(resolver.file_path)
                    elif simple not in STOP_TYPES["csharp"]:
                        keys = set().union(*(_splits(each) for each in static))
                        target.keys = _merged(target.keys, [keys])
                        target.types.add(simple)
                        resolver.imports.setdefault(simple, set()).add(scoped_pin(_ANY_CS_FILE, _clean(keys)))
            if target:
                self.found.append((name, target.pins(), False))
            else:
                self._bare(name, False)
            return
        if function.type == "conditional_access_expression":
            binding = next((c for c in function.named_children if c.type == "member_binding_expression"), None)
            member = binding.child_by_field_name("name") if binding is not None else None
            if member is not None:
                self._bare(_member_name(member, source), True)
            return
        if function.type != "member_access_expression":
            return
        member = function.child_by_field_name("name")
        owner = function.child_by_field_name("expression")
        if member is None or owner is None:
            return
        name = _member_name(member, source)
        if owner.type in ("this", "base"):
            target = self._own(name, start_at_bases=owner.type == "base")
            if target:
                self.found.append((name, target.pins(), False))
            else:
                self._bare(name, False)
            return
        if owner.type == "predefined_type":
            return
        static = None
        typed = None
        if owner.type == "alias_qualified_name" and _global(owner, source):
            # `global::Util.Run()`: a type of the global namespace.
            self._typed(_CsType(_member_name(owner.child_by_field_name("name"), source), None, "global::"), name,
                        False)
            return
        if owner.type in ("identifier", "generic_name"):
            text = _member_name(owner, source)
            if owner.type == "identifier" and (text in self.variables or self._field(text)[0]):
                typed = self.expr_type(owner)
                if typed is None:
                    self._bare(name, True)
                    return
            elif text[:1].isupper() or text in self.scope.aliases:
                static = _CsType(text)  # `T.M()`, `Svc<int>.M()`
            else:
                self._bare(name, True)
                return
        elif owner.type in ("member_access_expression", "qualified_name"):
            head = owner
            while head.type in ("member_access_expression", "qualified_name"):
                head = head.child_by_field_name("expression") or head.child_by_field_name("qualifier")
                if head is None:
                    break
            leftmost = _dotted(head, source) if head is not None and head.type in (
                "identifier", "generic_name", "alias_qualified_name") else None
            last = owner.child_by_field_name("name")
            if head is not None and head.type == "this":
                typed = self.expr_type(owner)
            elif (leftmost is not None and leftmost not in self.variables and not self._field(leftmost)[0]
                  and _leftmost(leftmost)[:1].isupper() and last is not None
                  and _member_name(last, source)[:1].isupper()):
                # A.B.Type.M(): a qualified type name.
                qualifier = _dotted(owner.child_by_field_name("expression") or owner.child_by_field_name("qualifier"),
                                    source)
                static = _CsType(_member_name(last, source), None, qualifier or "")
            if static is None and typed is None:
                self._bare(name, True)
                return
        else:
            typed = self.expr_type(owner)
            if typed is None:
                self._bare(name, True)
                return
        if static is not None:
            self._typed(static, name, False)
        else:
            self._typed(typed, name, True)


def _member_name(node: Node, source: bytes) -> str:
    """A member access's name, a generic method's type arguments left off."""
    if node.type == "generic_name":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return _text(ident, source)
    return _text(node, source)


def _referenced_types(root: Node, source: bytes) -> list[tuple[str, str, Node]]:
    """Every type the file writes as a type, as (qualifier, simple name, the
    node it is written at): a base, a field, property, parameter, local or
    return type, a `new T()`, a cast, a `typeof`, a generic argument; the
    `using`s themselves left out."""
    found: list[tuple[str, str, Node]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "using_directive":
            continue
        if node.type == "base_list":
            for base in node.named_children:
                found += [(qualifier, name, base) for qualifier, name in _type_refs(base, source)]
        for i, child in enumerate(node.children):
            if node.field_name_for_child(i) in ("type", "returns"):
                found += [(qualifier, name, child) for qualifier, name in _type_refs(child, source)]
        stack.extend(node.named_children)
    return found


#: ASP.NET Core's attribute-routing verbs and minimal-API map calls, by HTTP method.
_HTTP_ATTRIBUTES = {
    "HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpDelete": "DELETE", "HttpPatch": "PATCH",
    "HttpHead": "HEAD", "HttpOptions": "OPTIONS",
}
_MAP_CALLS = {"MapGet": "GET", "MapPost": "POST", "MapPut": "PUT", "MapDelete": "DELETE", "MapPatch": "PATCH"}


def _string_literal(node: Node | None, source: bytes) -> str | None:
    """A plain or verbatim string literal's value; None for anything else
    (an interpolated or raw string, a constant, `nameof`)."""
    if node is None:
        return None
    text = _text(node, source)
    if node.type == "string_literal" and text.startswith('"'):
        return text[1:-1]
    if node.type == "verbatim_string_literal":
        return text[2:-1].replace('""', '"')
    return None


_NO_TEMPLATE = ""  # an attribute given without a template


def _route_attributes(node: Node, source: bytes) -> dict[str, list[str | None]]:
    """A declaration's attributes by name (an `Attribute` suffix dropped),
    each as the template of every one of that name, in order: its first
    positional argument's string value (None: not a literal; "" when it has
    none)."""
    found: dict[str, list[str | None]] = {}
    for attribute_list in (c for c in node.children if c.type == "attribute_list"):
        for attribute in (a for a in attribute_list.named_children if a.type == "attribute"):
            name = attribute.child_by_field_name("name")
            if name is None:
                continue
            arguments = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None)
            values = [
                _string_literal(arg.named_children[0], source)
                for arg in (arguments.named_children if arguments is not None else [])
                if arg.type == "attribute_argument" and len(arg.named_children) == 1
                and arg.named_children[0].type != "assignment_expression"
            ]
            key = _text(name, source).rsplit(".", 1)[-1]
            key = key.removesuffix("Attribute") if key != "Attribute" else key
            found.setdefault(key, []).append(values[0] if values else _NO_TEMPLATE)
    return found


#: Minimal-API receivers: an app (a root), or a route builder a caller
#: passes, mounted under a prefix the file doesn't show.
_ROOT_BUILDER_TYPES = frozenset({"WebApplication"})
_SCOPE_TYPES = ("method_declaration", "local_function_statement", "constructor_declaration", "lambda_expression",
                "anonymous_method_expression", "compilation_unit")


def _aspnet_routes(root: Node, source: bytes, result: ExtractionResult, file_path: str, repo_id: str) -> None:
    """ASP.NET Core routes as Endpoints (apis/routes.py). Attribute routing:
    a controller's (`[ApiController]`, a `[Route]` or a `...Controller`
    name) `[HttpGet("x")]`/`[Route("x")]` actions under each of its
    `[Route]`s, the `[controller]` and `[action]` tokens filled in (any
    case), implemented by the action, owned by its class. Minimal APIs:
    `app.MapGet("/x", handler)`, implemented by a method of the file it
    names (a lambda implements nothing). The receiver is followed to its
    nearest declaration before the call in its scope (then the enclosing
    ones): `builder.Build()`, `WebApplication.Create...` or a
    `WebApplication` is the root; a literal `MapGroup("/x")` of a known one
    adds its prefix; anything else (an `IEndpointRouteBuilder` parameter)
    is mounted where the file doesn't show (`unmounted_endpoint`)."""
    functions: dict[str, set[str]] = {}
    for node in result.nodes:
        if node.label == "Function" and node.properties.get("file") == file_path:
            functions.setdefault(node.name, set()).add(node.properties["owner"])

    def fill(path: str, token: str, value: str) -> str:
        return re.sub(re.escape(f"[{token}]"), lambda _m: value, path, flags=re.IGNORECASE)

    def controller(node: Node, classes: list[str]) -> None:
        name = _text(node.child_by_field_name("name"), source)
        attributes = _route_attributes(node, source)
        if not ({"ApiController", "Route"} & set(attributes) or name.endswith("Controller")):
            return
        prefixes = attributes.get("Route") or [_NO_TEMPLATE]
        body = node.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            action = member.child_by_field_name("name")
            if member.type != "method_declaration" or action is None:
                continue
            action_name = _text(action, source)
            on_action = _route_attributes(member, source)
            routes = on_action.get("Route", [])
            # Each verb attribute's template; one with none takes each [Route]'s.
            verbs = [
                (verb, template)
                for attr, verb in _HTTP_ATTRIBUTES.items()
                for each in on_action.get(attr, [])
                for template in ([each] if each != _NO_TEMPLATE else routes or [_NO_TEMPLATE])
            ]
            if not verbs:
                verbs = [("*", template) for template in routes]
            seen: set[tuple[str, str]] = set()  # an absolute template is one route whatever the class's
            for verb, template in verbs:
                for prefix in prefixes:
                    if template is None or prefix is None:
                        continue  # not a literal
                    path = template.lstrip("~") if template.startswith(("/", "~/")) else join_route(prefix, template)
                    path = fill(path, "controller", name.removesuffix("Controller"))
                    path = join_route(fill(path, "action", action_name.removesuffix("Async")))
                    if (verb, path) not in seen:
                        seen.add((verb, path))
                        add_route(result, repo_id, file_path, "ASP.NET Core", verb, path, action_name,
                                  ".".join([*reversed(classes), name]))

    def classes_in(container: Node, classes: list[str]) -> None:
        for node in container.named_children:
            if node.type == "namespace_declaration" and (body := node.child_by_field_name("body")) is not None:
                classes_in(body, classes)
            elif node.type in _CLASS_LIKE and node.child_by_field_name("name") is not None:
                controller(node, classes)
                if (body := node.child_by_field_name("body")) is not None:
                    classes_in(body, [_text(node.child_by_field_name("name"), source), *classes])

    classes_in(root, [])

    def declaration(name: str, at: Node) -> tuple[str, Node] | None:
        """The nearest declaration of `name` before `at`: ("value", its
        initializer) or ("type", a parameter's type)."""
        node = at
        while node is not None:
            if node.type in _SCOPE_TYPES:
                found: tuple[int, Node | None] | None = None
                # Declarations and assignments of this scope (not of a scope in it).
                for each in [*_descendants(node, "variable_declarator"), *_descendants(node, "assignment_expression")]:
                    if each.type == "variable_declarator":
                        named = each.child_by_field_name("name")
                        value = each.named_children[-1] if len(each.named_children) > 1 else None
                    else:
                        named, value = each.child_by_field_name("left"), each.child_by_field_name("right")
                    if named is None or _text(named, source) != name or each.end_byte > at.start_byte:
                        continue
                    scope = each.parent
                    while scope is not None and scope.type not in _SCOPE_TYPES:
                        scope = scope.parent
                    if scope is None or (scope.start_byte, scope.end_byte) != (node.start_byte, node.end_byte):
                        continue
                    if found is None or each.start_byte > found[0]:
                        found = (each.start_byte, value)
                if found is not None:
                    return ("value", found[1]) if found[1] is not None else None
                params = node.child_by_field_name("parameters")
                for param in _descendants(params, "parameter") if params is not None else []:
                    named = param.child_by_field_name("name")
                    if named is not None and _text(named, source) == name:
                        declared = param.child_by_field_name("type")
                        return ("type", declared) if declared is not None else None
            node = node.parent
        return None

    def kind(receiver: Node, at: Node, depth: int = 0) -> tuple[str, str | None]:
        """("root", ""), ("prefix", it), ("unknown", None) or ("skip", None) (a dynamic prefix)."""
        if depth >= 8:
            return ("unknown", None)
        if receiver.type == "invocation_expression":
            node = receiver  # `app.MapGroup("/x").MapGet(...)`
        else:
            found = declaration(_text(receiver, source), at) if receiver.type == "identifier" else None
            if found is None:
                return ("unknown", None)
            how, node = found
            if how == "type":
                return ("root", "") if _text(node, source).rsplit(".", 1)[-1] in _ROOT_BUILDER_TYPES \
                    else ("unknown", None)
        # Down a configuring chain (`app.MapGroup("/x").WithTags(...)`) to its group or app.
        while node.type == "invocation_expression":
            func = node.child_by_field_name("function")
            if func is None or func.type != "member_access_expression":
                break
            method = _text(func.child_by_field_name("name"), source)
            inner = func.child_by_field_name("expression")
            if method == "Build" or _text(func, source).startswith("WebApplication."):
                return ("root", "")
            if method == "MapGroup":
                arguments = node.child_by_field_name("arguments")
                args = [a.named_children[-1] for a in arguments.named_children if a.type == "argument"] \
                    if arguments is not None else []
                mounted = _string_literal(args[0], source) if args else None
                parent = kind(inner, node, depth + 1)
                if mounted is None or parent[0] == "skip":
                    return ("skip", None)
                return ("prefix", join_route(parent[1], mounted)) if parent[0] in ("root", "prefix") \
                    else ("unknown", None)
            node = inner
        return ("unknown", None)

    for call in _descendants(root, "invocation_expression"):
        func = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if func is None or func.type != "member_access_expression" or arguments is None:
            continue
        receiver, method = func.child_by_field_name("expression"), func.child_by_field_name("name")
        values = [arg.named_children[-1] for arg in arguments.named_children if arg.type == "argument"]
        verb = _MAP_CALLS.get(_text(method, source)) if method is not None else None
        if receiver is None or verb is None or len(values) < 2:
            continue
        path = _string_literal(values[0], source)
        if path is None:
            continue
        where = kind(receiver, call)
        if where[0] == "skip":
            continue  # a group with a dynamic prefix
        mounted = where[0] in ("root", "prefix")
        full = join_route(where[1] or "", path)
        handler = values[1]
        if handler.type == "identifier" or (
            handler.type == "member_access_expression" and handler.child_by_field_name("name") is not None
        ):
            # A method group: the method of the file it names, when only one does.
            qualifier = None
            if handler.type == "member_access_expression":
                expression = handler.child_by_field_name("expression")
                qualifier = _text(expression, source).rsplit(".", 1)[-1] if expression is not None else ""
                handler = handler.child_by_field_name("name")
            name = _text(handler, source)
            owners = {o for o in functions.get(name, ()) if qualifier is None or o.rsplit(".", 1)[-1] == qualifier}
            if len(owners) == 1:
                add_route(result, repo_id, file_path, "ASP.NET Core", verb, full, name, next(iter(owners)),
                          mounted=mounted)
            else:
                add_route(result, repo_id, file_path, "ASP.NET Core", verb, full, name, pins=set(), mounted=mounted)
        else:
            add_route(result, repo_id, file_path, "ASP.NET Core", verb, full, mounted=mounted)


def extract_csharp_file(
    source_code: str, file_path: str, repo_id: str, config: ResolverConfig | None = None,
) -> ExtractionResult:
    """Parse a C# file and extract nodes and relationships.

    Args:
        source_code: The C# source code as a string.
        file_path: The Module node's identity — the file's path relative to
            the repo root, forward-slashed (e.g. 'MyApp/Services/Foo.cs').
        repo_id: Repository ID for scoping nodes.
        config: The repository's resolver configuration, read once per
            batch: the usings of the file's project
            (`ResolverConfig.cs_project`: its `<Using>` items and `global
            using`s). None, or a file with no project, has none.

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

    project = config.cs_project(file_path) if config is not None else None
    resolver = _CsResolver(
        root, source_bytes, file_path, (*project.usings, *project.global_usings) if project is not None else (),
    )
    sites = CallSites()
    # (label, name, owner) -> the match keys of every same-named Class or
    # Function of one owner declared here.
    match_keys: dict[tuple[str, str, str], set[str]] = {}

    def _record_calls(
        body: Node, caller_label: str, caller_name: str, classes: list[str], variables: dict,
    ) -> None:
        collector = _CsCalls(resolver, classes, variables, resolver.scope(body))
        # A local function can be called above its declaration.
        collector.locals |= {_text(name, source_bytes) for each in _descendants(body, "local_function_statement")
                             if (name := each.child_by_field_name("name")) is not None}
        collector.walk(body)
        # A method is owned by its classes; a class (a field initializer's
        # caller) by the classes around it; the Module by nothing.
        caller_owner = {"Function": ".".join(reversed(classes)), "Class": ".".join(reversed(classes[1:]))}.get(
            caller_label
        )
        for name, pins, member in collector.found:
            sites.add(caller_label, caller_name, classes[0] if classes else None, name, pins, member, caller_owner)

    def visit_block(container: Node, parent_name: str | None, parent_label: str, classes: list[str]) -> None:
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
                    visit_block(body, parent_name, parent_label, classes)
            elif node.type == "file_scoped_namespace_declaration":
                # No body field — everything after it in this same container
                # is already a sibling that visit_block will reach normally.
                continue
            elif node.type in _CLASS_LIKE:
                _visit_class(node, children, idx, parent_name, parent_label, classes)
            elif node.type in _METHOD_LIKE:
                _visit_method(node, children, idx, parent_name, parent_label, classes)
            elif node.type == "interface_declaration":
                continue  # scope cut — see module docstring
            else:
                # Other statement (field/property/top-level code) — attribute
                # any invocation expressions in it to the enclosing scope.
                _record_calls(node, parent_label, parent_name if parent_name else file_path, classes, {})

    def _visit_class(
        node: Node, container_children: list[Node], index: int, parent_name: str | None, parent_label: str,
        classes: list[str],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        decorators = _extract_attribute_names(node, source_bytes)
        base_names = _extract_base_list_names(node, source_bytes)

        owner = ".".join(reversed(classes))
        class_properties: dict = {
            "type": "class",
            "decorators": decorators,
            "file": file_path,
            "owner": owner,
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
        path = ".".join([*reversed(classes), class_name])
        match_keys.setdefault(("Class", class_name, owner), set()).add(f"{resolver.scope(node).namespace}:{path}")
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
                from_owner=".".join(reversed(classes[1:])) if parent_label == "Class" else None,
                to_owner=owner,
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
                    from_owner=owner,
                )
            )

        body = node.child_by_field_name("body")
        if body is not None:
            visit_block(body, class_name, "Class", [class_name, *classes])

    def _visit_method(
        node: Node, container_children: list[Node], index: int, parent_name: str | None, parent_label: str,
        classes: list[str],
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        decorators = _extract_attribute_names(node, source_bytes)

        owner = ".".join(reversed(classes))
        func_properties: dict = {
            "type": "function",
            "decorators": decorators,
            "file": file_path,
            "owner": owner,
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
        keys = match_keys.setdefault(("Function", func_name, owner), set())
        namespace = resolver.scope(node).namespace
        if classes:
            keys.add(f"{namespace}:{'.'.join(reversed(classes))}")
        params = node.child_by_field_name("parameters")
        first = next((p for p in params.named_children if p.type == "parameter"), None) if params is not None else None
        if first is not None and any(c.type == "modifier" and _text(c, source_bytes) == "this" for c in first.children):
            extended = _declared_type(first.child_by_field_name("type"), source_bytes)
            if extended is not None:
                # `this T x where T : class, IRepo`: an extension method of
                # IRepo, the first constraint naming a type.
                for clause in (c for c in node.named_children if c.type == "type_parameter_constraints_clause"):
                    target = next(iter(clause.named_children), None)
                    bound = next((found for c in clause.named_children if c.type == "type_parameter_constraint"
                                  if (found := _declared_type(c.child_by_field_name("type"), source_bytes))
                                  is not None), None)
                    if target is not None and _text(target, source_bytes) == extended.name and bound is not None:
                        extended = bound
                keys.add(f"{namespace}:+{extended.name}")  # an extension method of `extended`
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
                from_owner=".".join(reversed(classes[1:])) if parent_label == "Class" else None,
                to_owner=owner,
            )
        )

        body = node.child_by_field_name("body")  # block or arrow_expression_clause; None for abstract/interface
        if body is not None:
            variables = {}
            for param in params.named_children if params is not None else []:
                name = param.child_by_field_name("name")
                if param.type == "parameter" and name is not None:
                    variables[_text(name, source_bytes)] = _declared_type(param.child_by_field_name("type"), source_bytes)
            _record_calls(body, "Function", func_name, classes, variables)

    visit_block(root, None, "Module", [])
    result.relationships += sites.rows(file_path, repo_id)
    for each in result.nodes:
        if each.label in ("Class", "Function"):
            key = (each.label, each.name, each.properties["owner"])
            each.properties["mkeys"] = sorted(_clean(match_keys.get(key, ())))
    # IMPORTS: the classes the file references, in whichever C# file declares
    # them under a key the reference can mean, and the extension methods its
    # typed receivers' calls can reach.
    for qualifier, name, node in _referenced_types(root, source_bytes):
        scope = resolver.scope(node)
        resolved = resolver.type_name(name, scope) if not qualifier else name
        if (resolved is None or resolved in resolver.types or resolved in STOP_TYPES["csharp"]
                or _leftmost(qualifier) in STOP_TYPES["csharp"]):
            continue
        keys = resolver.type_keys(qualifier, name, scope, _enclosing_classes(node, source_bytes))
        resolver.imports.setdefault(resolved, set()).add(_key_pin(keys))
    for to_label, imports in (("Class", resolver.imports), ("Function", resolver.extension_imports)):
        for name, pins in sorted(imports.items()):
            if to_label == "Class" and name in resolver.types:
                continue
            result.relationships += call_rows(
                "Module", file_path, name, pins, False, None, file_path, repo_id, rel_type="IMPORTS",
                to_label=to_label,
            )
    _aspnet_routes(root, source_bytes, result, file_path, repo_id)
    # Project visibility: a node's project, and the projects a path-pinned
    # row of a file in a project may link nodes of.
    proj = _project_key(project.dir) if project is not None else ""
    for each in result.nodes:
        if each.label in ("Class", "Function"):
            each.properties["proj"] = proj
    if project is not None:
        visible = sorted({proj, *(_project_key(each) for each in project.references)})
        for rel in result.relationships:
            if rel.to_file is not None and pin_kind(rel.to_file) not in ("file", "fileless"):
                rel.visible = visible

    return own_edges(result, file_path)


def _project_key(folder: str) -> str:
    """A C# project's `proj`: its directory, "." for the repository root's,
    so it is never "", which a node of no project has."""
    return folder or "."


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
            one Module node — and the repository's C# projects give the
            file its project's usings. Falls back to the bare filename, and
            no project, when omitted.

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

    result = extract_csharp_file(source_code, module_name, repo_id, config)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
