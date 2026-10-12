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
    the global one; the usings of its namespace blocks and of the file; its
    project's `<Using>` items and `global using`s, read from the resolver
    configuration), an alias's exact target, a qualified `A.B.T`'s `A.B:T`
    (also under each enclosing namespace, and as a type nested in `A.B`), a
    nested `Ns:Outer.T` inside Outer; more than 32 keys drop the `@`
    (package confidence either way: two projects can each hold `Ns.T`).
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
  - A project's `<ProjectReference>`s are not read: a type of a project the
    writer's does not reference matches as well as one it does. Nor is
    `ImplicitUsings` (only System.* and Microsoft.* namespaces), and an
    enclosing namespace's type does not shadow a using's: both match.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Node, Parser

from devgraph.graph.schema import scoped_pin
from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, CallSites, call_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship, own_edges
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
    arguments and `global::` left off; None for anything else."""
    if node is None:
        return None
    kind = node.type
    if kind == "identifier":
        return _text(node, source)
    if kind == "generic_name":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        return _text(ident, source) if ident is not None else None
    if kind == "alias_qualified_name":
        return _dotted(node.child_by_field_name("name"), source)
    if kind in ("qualified_name", "member_access_expression"):
        head = _dotted(node.child_by_field_name("qualifier") or node.child_by_field_name("expression"), source)
        tail = _dotted(node.child_by_field_name("name"), source)
        return f"{head}.{tail}" if head is not None and tail is not None else None
    return None


def _key(dotted: str) -> str:
    """The match key of the type `dotted` names (`A.B.T`): `A.B:T`, `.:T`
    in the global namespace."""
    namespace, _dot, name = dotted.rpartition(".")
    return f"{namespace or _GLOBAL_NS}:{name}"


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
        return _type_refs(node.child_by_field_name("name"), source, qualifier)
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
        return _declared_type(node.child_by_field_name("name"), source, qualifier)
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
    namespace or type (dotted), `using static` and an alias."""
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
    usings in scope: the namespace blocks', the file's and its project's);
    `aliases` each using alias's target; `statics` the `using static`
    types."""

    chain: tuple[str, ...]
    namespaces: frozenset[str]
    aliases: dict[str, str]
    statics: tuple[str, ...]

    @property
    def namespace(self) -> str:
        return self.chain[0] if self.chain else _GLOBAL_NS


class _CsClass:
    def __init__(self) -> None:
        self.methods: set[str] = set()
        self.fields: dict[str, _CsType | None] = {}
        self.bases: list[tuple[str, frozenset[str]]] = []  # (simple name, match keys)
        self.keys: set[str] = set()  # its own match keys, `Ns:Outer.Name`
        self.partial = False


class _CsResolver:
    """What one C# file's names refer to: its classes (members, fields and
    bases, partial parts merged), its using aliases, the type parameters it
    declares, and each site's namespaces (`scope`)."""

    def __init__(self, root: Node, source: bytes, file_path: str, project_usings: tuple[CsUsing, ...] = ()) -> None:
        self.source = source
        self.file_path = file_path
        self.classes: dict[str, _CsClass] = {}
        self.types: set[str] = set()
        self.type_params: set[str] = set()
        self.methods: set[str] = set()  # every method name declared here
        self.imports: dict[str, set[str]] = {}  # class name -> IMPORTS pins
        self.extension_imports: dict[str, set[str]] = {}  # method name -> IMPORTS pins
        self._scopes: dict[int | None, _CsScope] = {}
        usings = [*project_usings, *(u for c in root.named_children if c.type == "using_directive"
                                     if (u := _using(c, source)) is not None)]
        file_ns = next((c for c in root.named_children if c.type == "file_scoped_namespace_declaration"), None)
        self._file_namespace = _dotted(file_ns.child_by_field_name("name"), source) if file_ns is not None else None
        self._unit_usings = usings
        every = usings + [u for c in _descendants(root, "using_directive") if (u := _using(c, source)) is not None]
        # A name written as a type, by its alias: the target's simple name.
        self.aliases: dict[str, str] = {u.alias: u.name.rpartition(".")[2] for u in every if u.alias}
        self._collect(root, ())
        for each in _descendants(root, "method_declaration"):
            name = each.child_by_field_name("name")
            if name is not None:
                self.methods.add(_text(name, source))

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
        names = [self._file_namespace] if self._file_namespace else []
        usings = list(self._unit_usings)
        for block in reversed(blocks):
            names.append(_dotted(block.child_by_field_name("name"), self.source) or "")
            body = block.child_by_field_name("body")
            usings += [u for c in (body.named_children if body is not None else [])
                       if c.type == "using_directive" if (u := _using(c, self.source)) is not None]
        parts = [part for name in names for part in name.split(".") if part]
        chain = tuple(".".join(parts[:i]) for i in range(len(parts), 0, -1))
        found = _CsScope(
            chain,
            frozenset({*chain, _GLOBAL_NS, *(u.name for u in usings if not u.static and not u.alias)}),
            {u.alias: u.name for u in usings if u.alias},
            tuple(sorted({u.name for u in usings if u.static and not u.alias})),
        )
        self._scopes[cache] = found
        return found

    def type_keys(self, qualifier: str, name: str, scope: _CsScope, classes: list[str]) -> set[str]:
        """The match keys the type `qualifier.name`, written at a site in
        `scope` inside `classes` (innermost first), can have: an alias's
        target; unqualified, the name in each of the site's namespaces and
        nested in each enclosing class (`Ns:Outer.Inner`); qualified `A.B.T`,
        `A.B:T` relative to the global namespace and to each enclosing one,
        and `T` nested in a type `A.B` of the site's namespaces."""
        if not qualifier:
            if name in scope.aliases:
                return {_key(scope.aliases[name])}
            path = list(reversed(classes))
            return {f"{namespace}:{name}" for namespace in scope.namespaces} | {
                f"{scope.namespace}:{'.'.join([*path[:i], name])}" for i in range(1, len(path) + 1)
            }
        head, _dot, rest = qualifier.partition(".")
        if head in scope.aliases:
            return {_key(".".join(part for part in (scope.aliases[head], rest, name) if part))}
        return {f"{qualifier}:{name}"} | {f"{prefix}.{qualifier}:{name}" for prefix in scope.chain} | {
            f"{namespace}:{qualifier}.{name}" for namespace in scope.namespaces
        }

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
                cls.keys.add(f"{scope.namespace}:{'.'.join([*path, _text(name, source)])}")
                cls.partial |= any(c.type == "modifier" and _text(c, source) == "partial" for c in child.children)
                base_list = next((c for c in child.named_children if c.type == "base_list"), None)
                for base in base_list.named_children if base_list is not None else []:
                    refs = _type_refs(base, source)
                    if refs:
                        qualifier, base_name = refs[0]
                        cls.bases.append(
                            (base_name, frozenset(self.type_keys(qualifier, base_name, scope, list(reversed(path)))))
                        )
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
        for each in _descendants(node, "type_parameter"):
            name = each.child_by_field_name("name")
            if name is not None:
                self.type_params.add(_text(name, source))

    def type_name(self, name: str) -> str | None:
        """A name written as a type, as the class it names: an alias's
        target; None for a type parameter or no class at all."""
        name = self.aliases.get(name, name)
        if name in self.type_params or name in _NOT_TYPES:
            return None
        return name

    def type_target(
        self, written: _CsType, method: str, scope: _CsScope, classes: list[str], instance: bool,
    ) -> tuple[set[str], set[str]] | None:
        """Where `method` of the type `written` is, as (files, match keys):
        a class declared here, as `class_target` finds it (this file for
        another type declared here); another file's type by its keys
        (`type_keys`), and on an instance also the extension methods of it
        the site's namespaces hold (`Ns:+T`), which the file then imports;
        None for a library type."""
        resolved = self.type_name(written.name) if not written.qualifier else written.name
        if resolved is None or resolved == "[]" or resolved in STOP_TYPES["csharp"]:
            return None
        if any(part in STOP_TYPES["csharp"] for part in written.qualifier.split(".")):
            return None  # a library type's member (`DateTime.UtcNow`)
        files: set[str] = set()
        keys: set[str] = set()
        if not written.qualifier and resolved in self.types:
            files, keys = self.class_target(resolved, method) if resolved in self.classes else (set(), set())
            if not files and not keys and not instance:
                files = {self.file_path}
        else:
            keys = self.type_keys(written.qualifier, written.name, scope, classes)
            self.imports.setdefault(resolved, set()).add(scoped_pin(_ANY_CS_FILE, keys))
        if instance:
            extensions = {f"{namespace}:+{resolved}" for namespace in scope.namespaces}
            keys |= extensions
            if method not in self.methods:
                self.extension_imports.setdefault(method, set()).add(scoped_pin(_ANY_CS_FILE, extensions))
        return files, keys

    def class_target(
        self, cls: str, method: str, seen: frozenset = frozenset(), own: bool = True,
    ) -> tuple[set[str], set[str]]:
        """Where `method` of class `cls` (declared here) is, as (files, match
        keys): this file when the class (`own`) or an in-file base declares
        it; else its other bases' keys, and for a partial class its own
        keys (another part, in another file, can declare it)."""
        info = self.classes.get(cls)
        if info is None or cls in seen:
            return set(), set()
        if own and method in info.methods:
            return {self.file_path}, set()
        files: set[str] = set()
        keys: set[str] = set(info.keys) if own and info.partial else set()
        for base, base_keys in info.bases:
            if base in self.classes:
                found_files, found_keys = self.class_target(base, method, seen | {cls})
                files |= found_files
                keys |= found_keys
            elif self.type_name(base) is not None and self.type_name(base) not in STOP_TYPES["csharp"]:
                keys |= base_keys
        return files, keys


def _pins(files: set[str], keys: set[str]) -> list[str]:
    """A call site's pins: each file, and one pin to any C# file whose node
    has one of `keys` (with more than MAX_SCOPE_KEYS, any C# file)."""
    return sorted(files) + ([scoped_pin(_ANY_CS_FILE, keys)] if keys else [])


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

    def _own(self, name: str, start_at_bases: bool = False) -> tuple[set[str], set[str]]:
        """`name` called unqualified, or on `this`/`base`: the enclosing
        classes and their bases, innermost first, up to the first that has
        it in this file (a `base.` call starts at the bases)."""
        files: set[str] = set()
        keys: set[str] = set()
        for cls in self.classes:
            found_files, found_keys = self.resolver.class_target(cls, name, own=not start_at_bases)
            files |= found_files
            keys |= found_keys
            if files or start_at_bases:
                break
        return files, keys

    def _typed(self, written: _CsType, name: str, instance: bool) -> None:
        """A call of `name` on the type `written` (static) or an instance of it."""
        target = self.resolver.type_target(written, name, self.scope, self.classes, instance)
        if target is None:
            return
        if target[0] or target[1]:
            self.found.append((name, _pins(*target), False))
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
            files, keys = self._own(name)
            if not files:
                for static in self.scope.statics:
                    simple = static.rpartition(".")[2]
                    if simple in resolver.types:
                        files.add(resolver.file_path)
                    elif simple not in STOP_TYPES["csharp"]:
                        keys.add(_key(static))
                        resolver.imports.setdefault(simple, set()).add(scoped_pin(_ANY_CS_FILE, {_key(static)}))
            if files or keys:
                self.found.append((name, _pins(files, keys), False))
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
            files, keys = self._own(name, start_at_bases=owner.type == "base")
            if files or keys:
                self.found.append((name, _pins(files, keys), False))
            else:
                self._bare(name, False)
            return
        if owner.type == "predefined_type":
            return
        static = None
        typed = None
        if owner.type == "identifier":
            text = _text(owner, source)
            if text in self.variables or self._field(text)[0]:
                typed = self.expr_type(owner)
                if typed is None:
                    self._bare(name, True)
                    return
            elif text[:1].isupper() or text in resolver.aliases:
                static = _CsType(text)
            else:
                self._bare(name, True)
                return
        elif owner.type in ("member_access_expression", "qualified_name"):
            head = owner
            while head.type in ("member_access_expression", "qualified_name"):
                head = head.child_by_field_name("expression") or head.child_by_field_name("qualifier")
                if head is None:
                    break
            leftmost = _text(head, source) if head is not None and head.type == "identifier" else None
            last = owner.child_by_field_name("name")
            if head is not None and head.type == "this":
                typed = self.expr_type(owner)
            elif (leftmost is not None and leftmost not in self.variables and not self._field(leftmost)[0]
                  and leftmost[:1].isupper() and last is not None and _member_name(last, source)[:1].isupper()):
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
    # (label, name) -> the match keys of every same-named Class or Function declared here.
    match_keys: dict[tuple[str, str], set[str]] = {}

    def _record_calls(
        body: Node, caller_label: str, caller_name: str, classes: list[str], variables: dict,
    ) -> None:
        collector = _CsCalls(resolver, classes, variables, resolver.scope(body))
        collector.walk(body)
        for name, pins, member in collector.found:
            sites.add(caller_label, caller_name, classes[0] if classes else None, name, pins, member)

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
        path = ".".join([*reversed(classes), class_name])
        match_keys.setdefault(("Class", class_name), set()).add(f"{resolver.scope(node).namespace}:{path}")
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
        keys = match_keys.setdefault(("Function", func_name), set())
        namespace = resolver.scope(node).namespace
        if classes:
            keys.add(f"{namespace}:{'.'.join(reversed(classes))}")
        params = node.child_by_field_name("parameters")
        first = next((p for p in params.named_children if p.type == "parameter"), None) if params is not None else None
        if first is not None and any(c.type == "modifier" and _text(c, source_bytes) == "this" for c in first.children):
            extended = _declared_type(first.child_by_field_name("type"), source_bytes)
            if extended is not None:
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
            each.properties["mkeys"] = sorted(match_keys.get((each.label, each.name), ()))
    # IMPORTS: the classes the file references, in whichever C# file declares
    # them under a key the reference can mean, and the extension methods its
    # typed receivers' calls can reach.
    for qualifier, name, node in _referenced_types(root, source_bytes):
        resolved = resolver.type_name(name) if not qualifier else name
        if resolved is None or resolved in resolver.types or resolved in STOP_TYPES["csharp"]:
            continue
        keys = resolver.type_keys(qualifier, name, resolver.scope(node), _enclosing_classes(node, source_bytes))
        resolver.imports.setdefault(resolved, set()).add(scoped_pin(_ANY_CS_FILE, keys))
    for to_label, imports in (("Class", resolver.imports), ("Function", resolver.extension_imports)):
        for name, pins in sorted(imports.items()):
            if to_label == "Class" and name in resolver.types:
                continue
            result.relationships += call_rows(
                "Module", file_path, name, pins, False, None, file_path, repo_id, rel_type="IMPORTS",
                to_label=to_label,
            )

    return own_edges(result, file_path)


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
