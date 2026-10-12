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
  - CALLS and IMPORTS resolved through types (see
    docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md,
    C#). Nothing ties a namespace to a folder, nor (beyond convention) a
    class to a file named after it, and a file's text says neither where
    its namespace's other types are, so another file's type `T` is its
    class file by suffix, `/T.cs` (package confidence: two namespaces can
    each have a `User.cs`).
    CALLS, by the first tier that applies: an unqualified `M()`, `this.M()`
    or `base.M()` -> this file when an enclosing class declared here (or an
    in-file base) has M, else its other bases' class files, else a `using
    static` type's; a static `T.M()` (an alias's target type, a qualified
    `A.B.T.M()`) and a method on a typed receiver (a parameter, a field or
    property of the enclosing classes, `T x`, `var x = new T()`, `new
    T().M()`, a `foreach` element of a `List<T>` or array) -> this file
    when T is declared here and has M, else `/T.cs`, and for a receiver
    also `/TExtensions.cs` (an extension method's conventional class); a
    library type (calls.STOP_TYPES) or a predefined one links nothing;
    anything else, `dynamic` included, a bare-name edge, which a member
    call never ends at its caller and a stoplisted method
    (calls.STOP_METHODS["csharp"]) never gets.
    IMPORTS: an edge to each class the file writes as a type (a base, a
    member, parameter, local or return type, `new T()`, a cast, a generic
    argument, a static receiver, an alias's or used `using static` target),
    declared in any C# file (`/.?.cs^`, package), not in this one and no
    library type. A `using` of a namespace imports nothing.

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
  - Cross-project (`.csproj` reference) resolution is out of scope per the
    Implementation Plan's table for this language.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Node, Parser

from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, CallSites, call_rows
from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship, own_edges
from devgraph.indexer.resolver_config import ResolverConfig

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

#: An IMPORTS row's pin: a class declared in any C# file, vendored copies left out.
_ANY_CS_FILE = "/.?.cs^"
#: Type names that name no class (`var`, `dynamic`).
_NOT_TYPES = frozenset({"var", "dynamic", "nint", "nuint"})


class _CsType(NamedTuple):
    """A declared type: its simple name, and the element type a `foreach`
    over it yields (`List<User>`: `User`)."""

    name: str
    element: str | None = None


def _type_names(node: Node | None, source: bytes) -> list[str]:
    """Every simple type name a type expression mentions (`Dictionary<Key,
    List<Item>>`: Dictionary, Key, List, Item; `Shop.Domain.User`: User)."""
    if node is None:
        return []
    kind = node.type
    if kind == "identifier":
        return [_text(node, source)]
    if kind == "qualified_name":
        return _type_names(node.child_by_field_name("name"), source)
    if kind == "alias_qualified_name":
        return _type_names(node.child_by_field_name("name"), source)
    if kind == "generic_name":
        found = []
        for child in node.named_children:
            if child.type == "identifier":
                found.append(_text(child, source))
            elif child.type == "type_argument_list":
                for argument in child.named_children:
                    found += _type_names(argument, source)
        return found
    if kind in ("nullable_type", "array_type", "pointer_type", "ref_type", "scoped_type", "tuple_type",
                "tuple_element"):
        found = []
        for child in node.named_children:
            found += _type_names(child, source)
        return found
    return []


def _declared_type(node: Node | None, source: bytes) -> _CsType | None:
    """A variable's declared type as the class its members are on (`User?`:
    User; an array or a predefined type: none), with a generic's last type
    argument as the element a `foreach` yields."""
    if node is None:
        return None
    kind = node.type
    if kind == "identifier":
        return _CsType(_text(node, source)) if _text(node, source) not in _NOT_TYPES else None
    if kind in ("qualified_name", "alias_qualified_name"):
        return _declared_type(node.child_by_field_name("name"), source)
    if kind == "nullable_type":
        return _declared_type(node.child_by_field_name("type") or next(iter(node.named_children), None), source)
    if kind == "generic_name":
        name = next((c for c in node.named_children if c.type == "identifier"), None)
        arguments = next((c for c in node.named_children if c.type == "type_argument_list"), None)
        last = arguments.named_children[-1] if arguments is not None and arguments.named_children else None
        element = _declared_type(last, source)
        return _CsType(_text(name, source), element.name if element else None) if name is not None else None
    if kind == "array_type":
        element = _declared_type(node.child_by_field_name("type"), source)
        return _CsType("[]", element.name if element else None)
    return None


class _CsClass:
    def __init__(self) -> None:
        self.methods: set[str] = set()
        self.fields: dict[str, _CsType | None] = {}
        self.bases: list[str] = []


class _CsResolver:
    """What one C# file's names refer to: its classes (members, fields and
    bases, partial parts merged), its using aliases and `using static`
    types, and the type parameters it declares."""

    def __init__(self, root: Node, source: bytes, file_path: str) -> None:
        self.source = source
        self.file_path = file_path
        self.classes: dict[str, _CsClass] = {}
        self.types: set[str] = set()
        self.type_params: set[str] = set()
        self.aliases: dict[str, str] = {}
        self.static_usings: list[str] = []
        self.referenced: set[str] = set()  # type names the file uses (IMPORTS)
        self._collect(root)

    def _collect(self, node: Node) -> None:
        source = self.source
        for child in node.named_children:
            kind = child.type
            if kind == "using_directive":
                self._using(child)
            elif kind in _CLASS_LIKE + ("interface_declaration", "enum_declaration"):
                name = child.child_by_field_name("name")
                if name is None:
                    continue
                cls = self.classes.setdefault(_text(name, source), _CsClass())
                self.types.add(_text(name, source))
                base_list = next((c for c in child.named_children if c.type == "base_list"), None)
                for base in base_list.named_children if base_list is not None else []:
                    names = _type_names(base, source)
                    if names:
                        cls.bases.append(names[0])
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
                    self._collect(body)
            elif kind in ("namespace_declaration",):
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect(body)
        for each in _descendants(node, "type_parameter"):
            name = each.child_by_field_name("name")
            if name is not None:
                self.type_params.add(_text(name, source))

    def _using(self, node: Node) -> None:
        alias = node.child_by_field_name("name")
        target = next(
            (c for c in node.named_children
             if c.type in ("identifier", "qualified_name", "generic_name")
             and (alias is None or (c.start_byte, c.end_byte) != (alias.start_byte, alias.end_byte))),
            None,
        )
        if target is None:
            return
        names = _type_names(target, self.source)
        if not names:
            return
        if alias is not None:
            self.aliases[_text(alias, self.source)] = names[0]
        elif any(c.type == "static" for c in node.children):
            self.static_usings.append(names[0])

    def type_name(self, name: str) -> str | None:
        """A name written as a type, as the class it names: an alias's
        target; None for a type parameter or no class at all."""
        name = self.aliases.get(name, name)
        if name in self.type_params or name in _NOT_TYPES:
            return None
        return name

    def type_pins(self, name: str, instance: bool) -> list[str] | None:
        """A type's pins: this file when declared here, else its class file
        by suffix (and, for a method on an instance, its extension class
        file); None for a library type."""
        resolved = self.type_name(name)
        if resolved is None or resolved == "[]" or resolved in STOP_TYPES["csharp"]:
            return None
        if resolved in self.types:
            return [self.file_path]
        self.referenced.add(resolved)
        pins = [f"/{resolved}.cs^"]
        if instance:
            pins.append(f"/{resolved}Extensions.cs^")
        return pins

    def class_pins(self, cls: str, method: str, seen: frozenset = frozenset()) -> list[str] | None:
        """Where `method` of class `cls` (declared here) is: this file when
        the class or an in-file base declares it, else its other bases'
        class files; None when none of that applies."""
        info = self.classes.get(cls)
        if info is None or cls in seen:
            return None
        if method in info.methods:
            return [self.file_path]
        pins: list[str] = []
        for base in info.bases:
            if base in self.classes:
                pins += self.class_pins(base, method, seen | {cls}) or []
            else:
                pins += self.type_pins(base, False) or []
        return pins or None


def _descendants(node: Node, kind: str):
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            yield current
        stack.extend(current.named_children)


class _CsCalls:
    """One caller's call sites, resolved by the tiers (see the module
    docstring): (callee name, pins or None for a bare name, member call?)."""

    def __init__(self, resolver: _CsResolver, classes: list[str], variables: dict[str, _CsType | None]) -> None:
        self.resolver = resolver
        self.classes = classes  # the enclosing classes, innermost first
        self.variables = variables
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
                typed = _CsType(iterable.element) if iterable is not None and iterable.element else None
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

    def _own(self, name: str, start_at_bases: bool = False) -> list[str] | None:
        """`name` called unqualified, or on `this`/`base`: the enclosing
        classes and their bases (a `base.` call starts at the bases)."""
        resolver = self.resolver
        for cls in self.classes:
            if start_at_bases:
                info = resolver.classes.get(cls)
                pins = []
                for base in info.bases if info is not None else []:
                    pins += (resolver.class_pins(base, name) if base in resolver.classes
                             else resolver.type_pins(base, False)) or []
                return pins or None
            pins = resolver.class_pins(cls, name)
            if pins:
                return pins
        return None

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
            pins = self._own(name)
            if pins is None:
                for static in resolver.static_usings:
                    pins = (pins or []) + (resolver.type_pins(static, False) or [])
            if pins:
                self.found.append((name, pins, False))
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
            pins = self._own(name, start_at_bases=owner.type == "base")
            if pins:
                self.found.append((name, pins, False))
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
                static = text
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
                static = _member_name(last, source)  # A.B.Type.M(): a qualified type name
            if static is None and typed is None:
                self._bare(name, True)
                return
        else:
            typed = self.expr_type(owner)
            if typed is None:
                self._bare(name, True)
                return
        if static is not None:
            pins = resolver.type_pins(static, False)
            if pins:
                self.found.append((name, pins, False))
            return
        if typed.name in resolver.classes:
            pins = resolver.class_pins(typed.name, name)
            if pins:
                self.found.append((name, pins, False))
            else:
                self._bare(name, True)
            return
        pins = resolver.type_pins(typed.name, True)
        if pins:
            self.found.append((name, pins, False))


def _member_name(node: Node, source: bytes) -> str:
    """A member access's name, a generic method's type arguments left off."""
    if node.type == "generic_name":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return _text(ident, source)
    return _text(node, source)


def _referenced_types(root: Node, source: bytes) -> set[str]:
    """Every simple type name the file writes as a type: a base, a field,
    property, parameter, local or return type, a `new T()`, a cast, a
    `typeof`, a generic argument; the `using`s themselves left out."""
    found: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "using_directive":
            continue
        if node.type == "base_list":
            for base in node.named_children:
                found.update(_type_names(base, source))
        for i, child in enumerate(node.children):
            if node.field_name_for_child(i) in ("type", "returns"):
                found.update(_type_names(child, source))
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
            batch: the file's project (`ResolverConfig.cs_project`). Not
            read yet.

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

    resolver = _CsResolver(root, source_bytes, file_path)
    sites = CallSites()

    def _record_calls(
        body: Node, caller_label: str, caller_name: str, classes: list[str], variables: dict,
    ) -> None:
        collector = _CsCalls(resolver, classes, variables)
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
            params = node.child_by_field_name("parameters")
            for param in params.named_children if params is not None else []:
                name = param.child_by_field_name("name")
                if param.type == "parameter" and name is not None:
                    variables[_text(name, source_bytes)] = _declared_type(param.child_by_field_name("type"), source_bytes)
            _record_calls(body, "Function", func_name, classes, variables)

    visit_block(root, None, "Module", [])
    result.relationships += sites.rows(file_path, repo_id)
    # IMPORTS: the classes the file references, in whichever C# file declares them.
    for name in sorted(_referenced_types(root, source_bytes) | resolver.referenced):
        resolved = resolver.type_name(name)
        if resolved is None or resolved in resolver.types or resolved in STOP_TYPES["csharp"]:
            continue
        result.relationships += call_rows(
            "Module", file_path, resolved, {_ANY_CS_FILE}, False, None, file_path, repo_id,
            rel_type="IMPORTS", to_label="Class",
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
            one Module node. Falls back to the bare filename when omitted.

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

    result = extract_csharp_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
