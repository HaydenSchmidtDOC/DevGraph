"""Tree-sitter-based Python source-code extractor.

Parses a Python file using Tree-sitter's grammar-based parser and extracts:
  - Module (the file itself)
  - Classes with their base classes (inheritance)
  - Functions (at module level and within classes)
  - Imports (from/import statements)
  - Decorators (applied to classes/functions)

Tree-sitter over stdlib `ast` per the Implementation Plan: grammar-based,
incremental-parse friendly, and the only option that generalizes to
non-Python languages later.

Returns a structured result (list of dataclasses) describing nodes and
relationships that can be upserted into the graph via
GraphEngine.upsert_node/upsert_relationship. All nodes are keyed on
(repo_id, name) for idempotent incremental indexing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from devgraph.indexer.calls import STOP_METHODS, call_rows
from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    UNKNOWN_BASE,
    add_heir_keys,
    class_member_pin,
    member_owner,
    merge_same_nodes,
    own_edges,
    owned_pin,
    split_owned_pin,
)
from devgraph.indexer.python.resolve import Bindings, absolute_module, relative_module
from devgraph.indexer.resolver_config import ResolverConfig

logger = logging.getLogger(__name__)

_PY_LANGUAGE = Language(tspython.language())


def _make_parser() -> Parser:
    return Parser(_PY_LANGUAGE)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _dotted_name(node: Node, source: bytes) -> str:
    """Render an identifier/attribute node (e.g. `module.attr`) as dotted text."""
    if node.type in ("identifier", "dotted_name"):
        return _text(node, source)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        if obj is not None and attr is not None:
            return f"{_dotted_name(obj, source)}.{_text(attr, source)}"
    return _text(node, source)


def _extract_decorator_names(decorator_nodes: list[Node], source: bytes) -> list[str]:
    """Extract decorator names from a list of `decorator` nodes."""
    names = []
    for deco in decorator_nodes:
        # A `decorator` node wraps one child: identifier, attribute, or call.
        target = deco.named_children[0] if deco.named_children else None
        if target is None:
            continue
        if target.type == "call":
            func = target.child_by_field_name("function")
            if func is not None:
                names.append(_dotted_name(func, source))
        elif target.type in ("identifier", "attribute"):
            names.append(_dotted_name(target, source))
    return names


def _call_functions(body: Node) -> list[Node]:
    """The `function` field of every call expression in a function/class/
    module-level body, in source order.

    Does not descend into nested function/class definitions — those are
    walked separately by the caller so calls are attributed to the correct
    enclosing scope, not hoisted to the outer function. A call whose
    function is itself a call (`get_handler()()`) is left to that inner
    call, which the walk reaches on its own.
    """
    functions: list[Node] = []

    def walk(node: Node) -> None:
        if node.type in ("function_definition", "class_definition"):
            return  # nested scope — attributed separately by the caller
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None and func.type != "call":
                functions.append(func)
        for child in node.children:
            walk(child)

    walk(body)
    return functions


def _pure_dotted(node: Node, source: bytes) -> str | None:
    """`a`, `a.b.c` as text; None for anything that isn't plain names and dots."""
    if node.type == "identifier":
        return _text(node, source)
    if node.type == "attribute":
        obj = node.child_by_field_name("object")
        attr = node.child_by_field_name("attribute")
        head = _pure_dotted(obj, source) if obj is not None else None
        if head is not None and attr is not None:
            return f"{head}.{_text(attr, source)}"
    return None


#: Receivers that are literals: `"".join(...)`, `{}.get(...)`.
_LITERAL_RECEIVERS = frozenset({
    "string", "concatenated_string", "integer", "float", "true", "false", "none", "dictionary", "list", "set",
    "tuple", "list_comprehension", "dictionary_comprehension", "set_comprehension", "generator_expression",
})

#: Python's builtin names (3.13), as a fixed set so an edge never depends on
#: the interpreter that indexed it. A bare call of one links nothing.
_BUILTIN_NAMES = frozenset({
    "ArithmeticError", "AssertionError", "AttributeError", "BaseException", "BaseExceptionGroup",
    "BlockingIOError", "BrokenPipeError", "BufferError", "BytesWarning", "ChildProcessError",
    "ConnectionAbortedError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "DeprecationWarning", "EOFError", "Ellipsis", "EncodingWarning", "EnvironmentError", "Exception",
    "ExceptionGroup", "False", "FileExistsError", "FileNotFoundError", "FloatingPointError", "FutureWarning",
    "GeneratorExit", "IOError", "ImportError", "ImportWarning", "IndentationError", "IndexError",
    "InterruptedError", "IsADirectoryError", "KeyError", "KeyboardInterrupt", "LookupError", "MemoryError",
    "ModuleNotFoundError", "NameError", "None", "NotADirectoryError", "NotImplemented", "NotImplementedError",
    "OSError", "OverflowError", "PendingDeprecationWarning", "PermissionError", "ProcessLookupError",
    "PythonFinalizationError", "RecursionError", "ReferenceError", "ResourceWarning", "RuntimeError",
    "RuntimeWarning", "StopAsyncIteration", "StopIteration", "SyntaxError", "SyntaxWarning", "SystemError",
    "SystemExit", "TabError", "TimeoutError", "True", "TypeError", "UnboundLocalError", "UnicodeDecodeError",
    "UnicodeEncodeError", "UnicodeError", "UnicodeTranslateError", "UnicodeWarning", "UserWarning",
    "ValueError", "Warning", "ZeroDivisionError", "__build_class__", "__import__", "abs", "aiter", "all",
    "anext", "any", "ascii", "bin", "bool", "breakpoint", "bytearray", "bytes", "callable", "chr",
    "classmethod", "compile", "complex", "copyright", "credits", "delattr", "dict", "dir", "divmod",
    "enumerate", "eval", "exec", "exit", "filter", "float", "format", "frozenset", "getattr", "globals",
    "hasattr", "hash", "help", "hex", "id", "input", "int", "isinstance", "issubclass", "iter", "len",
    "license", "list", "locals", "map", "max", "memoryview", "min", "next", "object", "oct", "open", "ord",
    "pow", "print", "property", "quit", "range", "repr", "reversed", "round", "set", "setattr", "slice",
    "sorted", "staticmethod", "str", "sum", "super", "tuple", "type", "vars", "zip",
})

_TYPE_WRAPPERS = frozenset({"Optional", "typing.Optional", "Union", "typing.Union"})

#: Annotations that type nothing: a receiver annotated with one is untyped.
_UNTYPED_ANNOTATIONS = frozenset({"None", "Any", "object"})


def _types_something(name: str) -> bool:
    """False for `None`, `Any`, `typing.Any`, `t.Any` and `object`."""
    return name.rsplit(".", 1)[-1] not in _UNTYPED_ANNOTATIONS


def _type_names(node: Node | None, source: bytes) -> list[str]:
    """The class names an annotation can name: `X`, `m.X`, `X | None`,
    `Optional[X]`, `Union[X, Y]` and the same quoted. Anything else (a
    container like `list[X]`) names none."""
    if node is None:
        return []
    if node.type in ("type", "type_parameter"):
        return [name for child in node.named_children for name in _type_names(child, source)]
    if node.type in ("identifier", "attribute"):
        text = _pure_dotted(node, source)
        return [text] if text and _types_something(text) else []
    if node.type == "binary_operator":
        return _type_names(node.child_by_field_name("left"), source) + _type_names(
            node.child_by_field_name("right"), source
        )
    if node.type == "generic_type" and node.named_children:
        if _text(node.named_children[0], source) in _TYPE_WRAPPERS:
            return [name for child in node.named_children[1:] for name in _type_names(child, source)]
        return []
    if node.type == "subscript":
        value = node.child_by_field_name("value")
        if value is not None and _pure_dotted(value, source) in _TYPE_WRAPPERS:
            return [
                name for child in node.children_by_field_name("subscript") for name in _type_names(child, source)
            ]
        return []
    if node.type == "string":
        content = "".join(_text(c, source) for c in node.named_children if c.type == "string_content")
        parts = [part.strip() for part in content.split("|")]
        return [
            part for part in parts
            if part and _types_something(part) and all(seg.isidentifier() for seg in part.split("."))
        ]
    return []


def _constructed(node: Node | None, source: bytes) -> str | None:
    """`X` for a call `X(...)` or `m.X(...)` of a capitalised callable (read
    as a constructor), else None."""
    called = node.child_by_field_name("function") if node is not None and node.type == "call" else None
    name = _pure_dotted(called, source) if called is not None else None
    return name if name and name.rsplit(".", 1)[-1][:1].isupper() else None


def _local_types(
    func: Node, body: Node, source: bytes, fixtures: dict[str, list[str]] | None = None
) -> dict[str, list[str]]:
    """The class names a function's own variables are typed with: annotated
    parameters, a parameter named after one of this file's pytest
    `fixtures`, annotated assignments and `x = X(...)` (a capitalised
    callable, read as a constructor). Nested defs are not looked into."""
    types: dict[str, set[str]] = {}
    params = func.child_by_field_name("parameters")
    for param in params.named_children if params is not None else []:
        if param.type == "typed_parameter":
            name_node = next((c for c in param.named_children if c.type == "identifier"), None)
        elif param.type == "typed_default_parameter":
            name_node = param.child_by_field_name("name")
        else:
            name_node = param if param.type == "identifier" else param.child_by_field_name("name")
            if name_node is not None and fixtures and _text(name_node, source) in fixtures:
                types.setdefault(_text(name_node, source), set()).update(fixtures[_text(name_node, source)])
            continue
        if name_node is not None:
            types.setdefault(_text(name_node, source), set()).update(
                _type_names(param.child_by_field_name("type"), source)
            )

    def walk(node: Node) -> None:
        if node.type in ("function_definition", "class_definition", "lambda"):
            return
        if node.type == "assignment":
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                annotated = _type_names(node.child_by_field_name("type"), source)
                constructor = _constructed(node.child_by_field_name("right"), source)
                names = annotated or ([constructor] if constructor else [])
                if names:
                    types.setdefault(_text(left, source), set()).update(names)
        for child in node.children:
            walk(child)

    walk(body)
    return {name: sorted(found) for name, found in types.items() if found}


def _parameter_names(func: Node, source: bytes) -> set[str]:
    """Every parameter name of a function (`*args`/`**kwargs` included)."""
    names = set()
    params = func.child_by_field_name("parameters")
    for param in params.named_children if params is not None else []:
        if param.type == "identifier":
            names.add(_text(param, source))
            continue
        name_node = param.child_by_field_name("name")
        if name_node is None:
            name_node = next((c for c in param.named_children if c.type == "identifier"), None)
        if name_node is not None:
            names.add(_text(name_node, source))
    return names


def _fixture_types(root: Node, source: bytes) -> dict[str, list[str]]:
    """This file's module-level pytest fixtures and the class names their
    value is built with: a return annotation, or a returned or yielded
    `X(...)` or variable typed as in `_local_types`. A test's parameter of
    that name is the fixture's value, so it is typed by it."""
    fixtures: dict[str, list[str]] = {}
    for node in root.named_children:
        definition = node.child_by_field_name("definition") if node.type == "decorated_definition" else None
        if definition is None or definition.type != "function_definition":
            continue
        decorators = _extract_decorator_names([c for c in node.named_children if c.type == "decorator"], source)
        name_node = definition.child_by_field_name("name")
        body = definition.child_by_field_name("body")
        if name_node is None or body is None or not any(d.rsplit(".", 1)[-1] == "fixture" for d in decorators):
            continue
        local = _local_types(definition, body, source)
        found = set(_type_names(definition.child_by_field_name("return_type"), source))

        def walk(n: Node) -> None:
            if n.type in ("function_definition", "class_definition", "lambda"):
                return
            if n.type in ("return_statement", "yield"):
                for value in n.named_children:
                    if value.type == "identifier":
                        found.update(local.get(_text(value, source), []))
                    elif (constructor := _constructed(value, source)) is not None:
                        found.add(constructor)
            for child in n.children:
                walk(child)

        walk(body)
        if found:
            fixtures[_text(name_node, source)] = sorted(found)
    return fixtures


def _class_like(name: str) -> bool:
    """Whether a from-imported name reads as a class (CapWords), not an
    instance (`config`, `DEFAULTS`)."""
    return name[:1].isupper() and not name.isupper()


#: Compound statements, and their clauses and blocks, whose body runs in
#: the scope around them: a def under one is the scope's, as a direct one is.
_COMPOUND = frozenset({
    "if_statement", "elif_clause", "else_clause", "try_statement", "except_clause", "except_group_clause",
    "finally_clause", "with_statement", "for_statement", "while_statement", "match_statement", "case_clause", "block",
})


def _definitions(node: Node | None) -> list[Node]:
    """The class and function definitions (decorated or not) of a block, or
    of one compound statement in it, in source order: its direct ones and
    those under its compound statements (`if`/`elif`/`else`, `try`/`except`/
    `else`/`finally`, `with`, `for`, `while`, `match`/`case`), never those
    inside another definition."""
    found: list[Node] = []

    def walk(each: Node) -> None:
        for child in each.named_children:
            if child.type in ("function_definition", "class_definition", "decorated_definition"):
                found.append(child)
            elif child.type in _COMPOUND:
                walk(child)

    if node is not None:
        walk(node)
    return found


def _direct_defs(block: Node | None, source: bytes) -> set[str]:
    """Names of the functions a block defines (decorated or not), under its
    compound statements too (`_definitions`)."""
    names: set[str] = set()
    for node in _definitions(block):
        definition = node.child_by_field_name("definition") if node.type == "decorated_definition" else node
        if definition is not None and definition.type == "function_definition":
            name_node = definition.child_by_field_name("name")
            if name_node is not None:
                names.add(_text(name_node, source))
    return names


@dataclass
class _ClassInfo:
    methods: set[str] = field(default_factory=set)
    bases: list[str] = field(default_factory=list)
    # What its members are owned by (common.member_owner): its own owner
    # chain and its name, one per class of the name in the file.
    members_owners: set[str] = field(default_factory=set)

    def members_owner(self) -> str | None:
        """The owner of the class's members, None when several classes of
        the file share its name (their methods are told apart by nothing
        a call names)."""
        return next(iter(self.members_owners)) if len(self.members_owners) == 1 else None


def _class_table(root: Node, source: bytes) -> dict[str, _ClassInfo]:
    """Every class in the file, at any depth: its own methods and its bases.
    Same-named classes merge."""
    classes: dict[str, _ClassInfo] = {}

    def walk(node: Node, label: str, name: str | None, owner: str) -> None:
        if node.type in ("class_definition", "function_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                own = member_owner(label, name, owner)
                if node.type == "function_definition":
                    label, name, owner = "Function", _text(name_node, source), own
                else:
                    class_name = _text(name_node, source)
                    info = classes.setdefault(class_name, _ClassInfo())
                    info.methods |= _direct_defs(node.child_by_field_name("body"), source)
                    info.members_owners.add(member_owner("Class", class_name, own))
                    for base in _extract_base_class_names(node.child_by_field_name("superclasses"), source):
                        if base not in info.bases:
                            info.bases.append(base)
                    label, name, owner = "Class", class_name, own
        for child in node.children:
            walk(child, label, name, owner)

    walk(root, "Module", None, "")
    return classes


#: Class decorators that keep a class's members as written (`dataclass`
#: adds dunders only): any other may replace the class (`common.UNKNOWN_BASE`).
_SAFE_CLASS_DECORATORS = frozenset({"dataclass", "total_ordering", "final", "runtime_checkable", "unique"})
#: Calls whose result is no class (`T = TypeVar("T")`): no alias of one.
_NOT_CLASS_CALLS = frozenset({"TypeVar", "ParamSpec", "TypeVarTuple", "TypeAliasType"})
#: Right-hand sides that are no class: a type expression (`Literal[...]`,
#: `X | Y`, `Annotated[...]`) or a literal.
_NOT_CLASS_VALUES = frozenset({
    "subscript", "binary_operator", "string", "concatenated_string", "integer", "float", "true", "false", "none",
    "list", "dictionary", "tuple", "set", "lambda", "list_comprehension", "dictionary_comprehension",
    "set_comprehension", "generator_expression", "unary_operator", "comparison_operator", "boolean_operator",
})


def _heir_bases(superclasses: Node | None, source: bytes) -> list[str]:
    """A class's bases as common.heir_keys reads them: an identifier or a
    dotted name (a subscript's value, `Base[int]`), `metaclass=X`'s `X`
    (its methods are the class's too), and common.UNKNOWN_BASE for any
    other expression (`with_metaclass(...)`, `namedtuple(...)`, `X if c
    else Y`, `*bases`), which can supply any member."""
    found: list[str] = []
    for child in superclasses.named_children if superclasses is not None else []:
        if child.type == "keyword_argument":
            name, value = child.child_by_field_name("name"), child.child_by_field_name("value")
            if name is None or _text(name, source) != "metaclass":
                continue
            child = value
        elif child.type == "subscript":
            child = child.child_by_field_name("value")
        if child is not None and child.type in ("identifier", "attribute"):
            found.append(_dotted_name(child, source))
        else:
            found.append(UNKNOWN_BASE)
    return found


def _class_declarations(root: Node, source: bytes) -> list[tuple[str, str, list[str], set[str]]]:
    """Every class declaration of the file, each on its own (two nested
    `Meta`s of two classes stay apart), as common.heir_keys reads them:
    (name, its members' owner, its bases (`_heir_bases`), its methods); a
    class decorated otherwise than `_SAFE_CLASS_DECORATORS`, or rebound at
    the module level (`X = register(X)`), also of common.UNKNOWN_BASE,
    since the name may be another class. Then each module-level alias or
    instance of a class (`UserModel = User`, `Registry = _Registry()`, a
    CapWords name; not a type expression, a literal or a `TypeVar`) as a
    member-less declaration of that class, or of common.UNKNOWN_BASE when
    no class of the file is named, so a call through it links what it did
    before owners."""
    found: list[tuple[str, str, list[str], set[str]]] = []

    def walk(node: Node, label: str, name: str | None, owner: str) -> None:
        if node.type in ("class_definition", "function_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                own = member_owner(label, name, owner)
                if node.type == "function_definition":
                    label, name, owner = "Function", _text(name_node, source), own
                else:
                    class_name = _text(name_node, source)
                    bases = _heir_bases(node.child_by_field_name("superclasses"), source)
                    decorated = node.parent if node.parent is not None and node.parent.type == "decorated_definition" \
                        else None
                    decorators = _extract_decorator_names(
                        [c for c in decorated.named_children if c.type == "decorator"], source,
                    ) if decorated is not None else []
                    if any(each.rsplit(".", 1)[-1] not in _SAFE_CLASS_DECORATORS for each in decorators):
                        bases.append(UNKNOWN_BASE)
                    found.append((
                        class_name, member_owner("Class", class_name, own), bases,
                        _direct_defs(node.child_by_field_name("body"), source),
                    ))
                    label, name, owner = "Class", class_name, own
        for child in node.children:
            walk(child, label, name, owner)

    walk(root, "Module", None, "")
    classes = {each[0] for each in found}
    rebound: set[str] = set()
    for statement in root.named_children:
        assignment = statement.named_children[0] if statement.type == "expression_statement" and (
            statement.named_children) else None
        if assignment is None or assignment.type != "assignment":
            continue
        left, right = assignment.child_by_field_name("left"), assignment.child_by_field_name("right")
        if left is None or left.type != "identifier" or right is None:
            continue
        alias = _text(left, source)
        if alias in classes:
            if not (right.type == "identifier" and _text(right, source) == alias):
                rebound.add(alias)
            continue
        if not _class_like(alias) or right.type in _NOT_CLASS_VALUES:
            continue
        target = right.child_by_field_name("function") if right.type == "call" else right
        if right.type == "call" and target is not None and _text(target, source).rsplit(".", 1)[-1] in _NOT_CLASS_CALLS:
            continue
        named = _text(target, source) if target is not None and target.type == "identifier" else None
        found.append((alias, alias, [named if named in classes else UNKNOWN_BASE], set()))
    return [
        (name, owner, [*bases, UNKNOWN_BASE] if name in rebound and owner == name else bases, members)
        for name, owner, bases, members in found
    ]


@dataclass
class _Scope:
    """What a call's names can mean where it is made: the functions defined
    in this scope, its variables' types, and the class `self` belongs to."""

    defs: set[str]
    types: dict[str, list[str]]
    cls: str | None
    parent: _Scope | None = None
    params: set[str] = field(default_factory=set)

    def defines(self, name: str) -> bool:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.defs:
                return True
            scope = scope.parent
        return False

    def is_parameter(self, name: str) -> bool:
        """A parameter of this or an enclosing function, not redefined by a
        nearer def."""
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.defs:
                return False
            if name in scope.params:
                return True
            scope = scope.parent
        return False

    def types_of(self, name: str) -> list[str] | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.types:
                return scope.types[name]
            scope = scope.parent
        return None


class _CallResolver:
    """Resolves a call to the files its callee can be in (see the module
    docstring): each target is (callee name, pin), the pin a file, a package
    directory ending in "/", or None for a bare name match."""

    def __init__(self, file_path: str, bindings: Bindings, classes: dict[str, _ClassInfo], source: bytes):
        self.file_path = file_path
        self.bindings = bindings
        self.classes = classes
        self.source = source

    def _module_pins(self, receiver: str) -> list[str] | None:
        """Where a name read off an imported module or a from-imported name
        can be, as owned pins (common.owned_pin): a module's attribute is a
        top-level one (owner ""), and so is a submodule's; one read off a
        from-imported class (a CapWords name, `User.create()`) is the
        class's member, its own or one it inherits from a class of its file
        (common.heir_keys); one read off any other from-imported name (an
        instance) is any of the name in its module."""
        ref = self.bindings.modules.get(receiver)
        if ref is not None:
            return [owned_pin(pin, "") for pin in ref.files() + ref.dirs()]
        symbol = self.bindings.symbols.get(receiver)
        if symbol is not None:
            owner = symbol.name if _class_like(symbol.name) else None
            return [class_member_pin(pin, owner) for pin in symbol.module.files() + symbol.module.dirs()] + [
                owned_pin(pin, "") for pin in symbol.submodule.files() + symbol.submodule.dirs()
            ]
        return None

    def _class_pins(self, type_name: str, method: str) -> list[str] | None:
        """Where `method` of the class `type_name` (as written) can be. An
        in-file class is walked like `self.method()` (see _method_pins); an
        imported one's method is the class's member in its files (its own,
        or one it inherits from a class of its file, common.heir_keys); a
        class read off a from-imported name (`x.User`) any of the name
        there."""
        if type_name in self.classes:
            return self._method_pins(type_name, method, own=True)
        if "." in type_name:
            head, cls = type_name.rsplit(".", 1)
            pins = self._module_pins(head)
            if pins is None:
                return None
            # A module's top-level class (owner "" pins) owns its members.
            return [class_member_pin(base, cls if owner == "" else None) for base, owner in map(split_owned_pin, pins)]
        symbol = self.bindings.symbols.get(type_name)
        if symbol is not None:
            return [class_member_pin(pin, symbol.name) for pin in symbol.module.files() + symbol.module.dirs()]
        return None

    def _method_pins(self, cls: str, method: str, own: bool) -> list[str] | None:
        """Where `self.method()` (own) or `super().method()` in `cls` can go:
        this file when `cls` or an in-file base defines it, as that class's
        member (common.owned_pin), and the files of each imported base (not
        followed further). None when nothing does."""
        if own and method in self.classes[cls].methods:
            return [owned_pin(self.file_path, self.classes[cls].members_owner())]
        pins: set[str] = set()
        found = False
        seen = {cls}

        def bases_of(name: str) -> None:
            nonlocal found
            for base in self.classes[name].bases:
                if base in self.classes:
                    if base in seen:
                        continue
                    seen.add(base)
                    if method in self.classes[base].methods:
                        pins.add(owned_pin(self.file_path, self.classes[base].members_owner()))
                        found = True
                    else:
                        bases_of(base)
                elif (base_pins := self._class_pins(base, method)) is not None:
                    pins.update(base_pins)
                    found = True

        bases_of(cls)
        return sorted(pins) if found else None

    def _receiver_pins(self, receiver: str, method: str, scope: _Scope) -> list[str] | None:
        if "." not in receiver:
            types = scope.types_of(receiver)
            if types:
                found = [pins for name in types if (pins := self._class_pins(name, method)) is not None]
                if found:
                    return sorted({pin for pins in found for pin in pins})
            if receiver in self.classes:
                return self._class_pins(receiver, method)
        if scope.is_parameter(receiver.split(".", 1)[0]):
            return None  # a parameter shadows the import of its name
        return self._module_pins(receiver)

    def resolve(self, func: Node, scope: _Scope) -> list[tuple[str, str | None]]:
        source = self.source
        if func.type == "identifier":
            name = _text(func, source)
            if scope.defines(name):
                return [(name, self.file_path)]
            if scope.is_parameter(name):
                return []  # calling a parameter: nothing to resolve, and it shadows any import
            symbol = self.bindings.symbols.get(name)
            # A from-imported (or star-imported) name is a module's top-level one.
            if symbol is not None:
                return [(symbol.name, owned_pin(pin, "")) for pin in symbol.module.files() + symbol.module.dirs()]
            if name in self.bindings.modules or name in _BUILTIN_NAMES:
                return []
            return [(name, owned_pin(pin, "")) for star in self.bindings.stars for pin in star.files() + star.dirs()]
        if func.type != "attribute":
            return []
        obj = func.child_by_field_name("object")
        attr_node = func.child_by_field_name("attribute")
        if obj is None or attr_node is None:
            return []
        attr = _text(attr_node, source)
        own_receiver = obj.type == "identifier" and _text(obj, source) in ("self", "cls")
        super_call = (
            obj.type == "call"
            and (inner := obj.child_by_field_name("function")) is not None
            and inner.type == "identifier"
            and _text(inner, source) == "super"
        )
        if (own_receiver or super_call) and scope.cls in self.classes:
            pins = self._method_pins(scope.cls, attr, own=own_receiver)
            if pins is not None:
                return [(attr, pin) for pin in pins]
        elif (receiver := _pure_dotted(obj, source)) is not None:
            pins = self._receiver_pins(receiver, attr, scope)
            if pins is not None:
                return [(attr, pin) for pin in pins]
        elif (constructor := _constructed(obj, source)) is not None:
            pins = self._class_pins(constructor, attr)  # `Fake().run()`
            if pins is not None:
                return [(attr, pin) for pin in pins]
        if obj.type in _LITERAL_RECEIVERS or attr in STOP_METHODS["python"]:
            return []
        return [(attr, None)]


def _extract_docstring(body: Node, source: bytes) -> str | None:
    """Extract a class/function/module's docstring, if its body's first
    statement is Python's docstring convention: an expression_statement
    wrapping a bare string node.
    """
    if not body.named_children:
        return None
    first = body.named_children[0]
    if first.type != "expression_statement" or not first.named_children:
        return None
    string_node = first.named_children[0]
    if string_node.type != "string":
        return None
    raw = _text(string_node, source)
    return _clean_docstring(raw)


def _clean_docstring(raw: str) -> str:
    """Strip a string literal's quote characters and common leading indentation."""
    text = raw.strip()
    for prefix in ('"""', "'''"):
        if text.startswith(prefix) and text.endswith(prefix) and len(text) >= 2 * len(prefix):
            text = text[len(prefix) : -len(prefix)]
            break
    else:
        for prefix in ('"', "'"):
            if text.startswith(prefix) and text.endswith(prefix) and len(text) >= 2:
                text = text[1:-1]
                break

    lines = text.split("\n")
    # Dedent using the minimum indentation of non-blank lines after the first
    # (PEP 257: the first line typically starts right after the opening quote).
    non_first = [line for line in lines[1:] if line.strip()]
    if non_first:
        indent = min(len(line) - len(line.lstrip()) for line in non_first)
        lines = [lines[0]] + [line[indent:] if len(line) >= indent else line for line in lines[1:]]
    return "\n".join(lines).strip()


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """Compute a PEP-257 summary line: first line up to the first blank line
    or terminating period, with a hard fallback truncation for docstrings
    that don't follow that convention.
    """
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


def _extract_base_class_names(superclasses_node: Node | None, source: bytes) -> list[str]:
    """Extract base class names from an `argument_list` node under class_definition."""
    if superclasses_node is None:
        return []
    names = []
    for child in superclasses_node.named_children:
        if child.type == "subscript":  # `Base[int]`, `typing.Generic[T]`: the base is its value
            child = child.child_by_field_name("value") or child
        if child.type in ("identifier", "attribute"):
            names.append(_dotted_name(child, source))
        elif child.type == "keyword_argument":
            # e.g. `class Foo(metaclass=Meta):` — not a real base class.
            continue
    return names


def _extract_imports(root: Node, source: bytes, file_path: str, roots: tuple[str, ...] = ()) -> Bindings:
    """Every import statement in the file, function-local ones included, as
    its bindings and IMPORTS targets (see resolve.py).

    `file_path` is the importing file's repo-relative path with forward
    slashes ('services/api/main.py', or 'main.py' at the repo root): absolute
    imports are tried under each of its ancestor directories and the
    configured `roots` (`src/` directories beside them, pyproject roots), and relative ones against its own directory. Each import targets every candidate
    file, `p.py` and `p/__init__.py`; only the ones that exist as Modules
    ever become edges. `from P import n` also targets the submodule `P.n`.
    """
    bindings = Bindings(roots=roots)
    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""

    def walk(node: Node) -> None:
        if node.type == "import_statement":
            # import X [as Y] [, Z [as W]]
            for child in node.named_children:
                if child.type == "dotted_name":
                    bindings.add_import(_text(child, source), None, file_path)
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        alias = _text(alias_node, source) if alias_node else None
                        bindings.add_import(_text(name_node, source), alias, file_path)
        elif node.type == "import_from_statement":
            # from X import Y [as Z][, ...] | from . import Y | from X import *
            module_node = node.child_by_field_name("module_name")
            module_name = _text(module_node, source) if module_node else ""
            # tree_sitter's Python bindings hand back a fresh Node wrapper
            # object on every accessor call, so `child is module_node`
            # never matches even for the same underlying tree node —
            # compare byte spans instead to keep the module-name
            # dotted_name out of the imported names.
            module_span = (module_node.start_byte, module_node.end_byte) if module_node else None

            names: list[tuple[str, str | None]] = []
            star = False
            for child in node.named_children:
                if child.type == "dotted_name" and (child.start_byte, child.end_byte) != module_span:
                    names.append((_text(child, source), None))
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is not None:
                        names.append((_text(name_node, source), _text(alias_node, source) if alias_node else None))
                elif child.type == "wildcard_import":
                    star = True

            if module_name.startswith("."):
                bindings.add_from(relative_module(module_name, current_dir), names, star)
            elif module_name:
                bindings.add_from(absolute_module(module_name, file_path, roots), names, star)

        for child in node.children:
            walk(child)

    walk(root)
    return bindings


def extract_python_file(
    source_code: str, file_path: str, repo_id: str, config: ResolverConfig | None = None,
) -> ExtractionResult:
    """Parse a Python file and extract nodes and relationships.

    Args:
        source_code: The Python source code as a string.
        file_path: The Module node's identity — should be the file's path
            relative to the repo root, using forward slashes (e.g.
            'services/api/main.py', or just 'main.py' for a repo-root file).
            A bare filename with no directory also works but loses the
            ability to resolve multi-level relative imports and risks
            colliding with a same-named file elsewhere in the repo (two
            files both named 'main.py' in different directories would
            otherwise merge into one Module node) — index_file() computes
            the correct repo-relative form automatically.
        repo_id: Repository ID for scoping nodes.
        config: The repository's resolver configuration (its pyproject.toml
            files and `src/` directories), read once per batch: the source
            roots are import roots of the files they apply to. None reads no
            roots besides each ancestor directory.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (it uses
        ERROR nodes rather than raising), so partial results are extracted
        and the error is logged rather than the whole file being skipped.
    """
    result = ExtractionResult()
    source_bytes = source_code.encode("utf-8")

    parser = _make_parser()
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        logger.warning(f"Syntax errors while parsing {file_path}; extracting best-effort result")

    # Create a Module node for the file itself.
    module_properties: dict = {"type": "module", "source_file": file_path}
    module_docstring = _extract_docstring(root, source_bytes)
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

    # IMPORTS edges to every candidate file of every import (see resolve.py).
    # file_path is expected to be the repo-relative path with forward
    # slashes (e.g. 'services/api/main.py'), which the candidates hang off.
    # A package's `from . import x` names its own __init__.py, never an edge.
    roots = config.python_roots(file_path) if config is not None else ()
    bindings = _extract_imports(root, source_bytes, file_path, roots)
    for target in sorted(bindings.targets - {file_path}):
        result.relationships.append(
            GraphRelationship(
                from_label="Module",
                from_name=file_path,
                rel_type="IMPORTS",
                to_label="Module",
                to_name=target,
                repo_id=repo_id,
            )
        )

    class_table = _class_table(root, source_bytes)
    resolver = _CallResolver(file_path, bindings, class_table, source_bytes)
    fixtures = _fixture_types(root, source_bytes)
    # (caller label, caller name, caller owner) -> callee name -> [pins, bare?, caller classes]
    calls: dict[tuple[str, str, str | None], dict[str, list]] = {}

    def record_calls(
        caller_label: str, caller_name: str, body: Node, scope: _Scope, caller_class: str | None,
        caller_owner: str | None,
    ) -> None:
        # caller_class records the enclosing class of a method-body call (None
        # for module-level/free-function calls) so find_callers can optionally
        # narrow results via scope_to_class — an opt-in query-time filter
        # (Implementation Plan #3, Item 2). caller_owner tells same-named
        # callers of the file apart (None for the Module).
        by_name = calls.setdefault((caller_label, caller_name, caller_owner), {})
        for func in _call_functions(body):
            for name, pin in resolver.resolve(func, scope):
                entry = by_name.setdefault(name, [set(), False, set()])
                if pin is None:
                    entry[1] = True
                else:
                    entry[0].add(pin)
                if caller_class:
                    entry[2].add(caller_class)

    def visit_block(block: Node, parent_name: str | None, parent_label: str, scope: _Scope, parent_owner: str) -> None:
        """Visit statements in a block (module body, class body, function body)
        whose own owner is `parent_owner`. A class body's defs are methods:
        they see the class's enclosing scope."""
        def_scope, def_cls = (scope.parent, parent_name) if parent_label == "Class" else (scope, scope.cls)
        for node in block.named_children:
            if node.type in ("class_definition", "function_definition", "decorated_definition"):
                visit_definition(node, parent_name, parent_label, scope, def_scope, def_cls, parent_owner)
                continue
            # Module/class-body-level statement (not a def) — attribute
            # any call expressions in it to the enclosing scope (usually
            # the Module, for top-level script code / constant setup).
            caller_class = parent_name if parent_label == "Class" else None
            record_calls(
                parent_label, parent_name if parent_name else file_path, node, scope, caller_class,
                parent_owner if parent_label != "Module" else None,
            )
            # A def under a compound statement (`try:`, `if`, `with`, ...)
            # is the block's own, as a direct one is.
            for definition in _definitions(node) if node.type in _COMPOUND else []:
                visit_definition(definition, parent_name, parent_label, scope, def_scope, def_cls, parent_owner)

    def visit_definition(
        node: Node, parent_name: str | None, parent_label: str, scope: _Scope, def_scope: _Scope,
        def_cls: str | None, parent_owner: str,
    ) -> None:
        """Visit one class or function definition (decorated or not) of a
        block: a class sees `scope`, a function `def_scope` with `self` of
        `def_cls`."""
        decorators: list[Node] = []
        if node.type == "decorated_definition":
            # decorated_definition wraps decorator(s) + the actual definition.
            decorators = [c for c in node.named_children if c.type == "decorator"]
            node = node.child_by_field_name("definition")
        if node is not None and node.type == "class_definition":
            _visit_class(node, parent_name, parent_label, scope, parent_owner, extra_decorators=decorators)
        elif node is not None and node.type == "function_definition":
            _visit_function(
                node, parent_name, parent_label, def_scope, def_cls, parent_owner, extra_decorators=decorators,
            )

    def _visit_class(
        node: Node,
        parent_name: str | None,
        parent_label: str,
        scope: _Scope,
        parent_owner: str,
        extra_decorators: list[Node] | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)

        decorator_nodes = extra_decorators or []
        decorators = _extract_decorator_names(decorator_nodes, source_bytes)

        superclasses_node = node.child_by_field_name("superclasses")
        base_classes = _extract_base_class_names(superclasses_node, source_bytes)

        owner = member_owner(parent_label, parent_name, parent_owner)
        class_properties: dict = {
            "type": "class",
            "decorators": decorators,
            "file": file_path,
            "owner": owner,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        body_node = node.child_by_field_name("body")
        if body_node is not None:
            docstring = _extract_docstring(body_node, source_bytes)
            if docstring:
                class_properties["description"] = _docstring_summary(docstring)
                class_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(
                label="Class",
                repo_id=repo_id,
                name=class_name,
                properties=class_properties,
            )
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
                from_owner=parent_owner if parent_label != "Module" else None,
                to_owner=owner,
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
                    from_owner=owner,
                )
            )

        if body_node is not None:
            visit_block(body_node, class_name, "Class", _Scope(set(), {}, None, scope), owner)

    def _visit_function(
        node: Node,
        parent_name: str | None,
        parent_label: str,
        enclosing: _Scope,
        cls: str | None,
        parent_owner: str,
        extra_decorators: list[Node] | None = None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        func_name = _text(name_node, source_bytes)

        decorator_nodes = extra_decorators or []
        decorators = _extract_decorator_names(decorator_nodes, source_bytes)

        owner = member_owner(parent_label, parent_name, parent_owner)
        func_properties: dict = {
            "type": "function",
            "decorators": decorators,
            "file": file_path,
            "owner": owner,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        body_node = node.child_by_field_name("body")
        if body_node is not None:
            docstring = _extract_docstring(body_node, source_bytes)
            if docstring:
                func_properties["description"] = _docstring_summary(docstring)
                func_properties["docstring_full"] = docstring

        result.nodes.append(
            GraphNode(
                label="Function",
                repo_id=repo_id,
                name=func_name,
                properties=func_properties,
            )
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
                from_owner=parent_owner if parent_label != "Module" else None,
                to_owner=owner,
            )
        )

        # Nested function and class definitions (under compound statements
        # too), and CALLS edges for every call expression made directly in
        # this function's body (not inside a nested def — _call_functions
        # stops descending at nested function/class scopes so those calls
        # get attributed to the nested definition itself, not hoisted here).
        if body_node is not None:
            scope = _Scope(
                _direct_defs(body_node, source_bytes), _local_types(node, body_node, source_bytes, fixtures), cls,
                enclosing, _parameter_names(node, source_bytes),
            )
            caller_class = parent_name if parent_label == "Class" else None
            record_calls("Function", func_name, body_node, scope, caller_class, owner)

            for child in _definitions(body_node):
                visit_definition(child, func_name, "Function", scope, scope, cls, owner)

    visit_block(root, None, "Module", _Scope(_direct_defs(root, source_bytes), {}, None), "")
    for (caller_label, caller_name, caller_owner), by_name in sorted(
        calls.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
    ):
        for name, (pins, bare, classes) in sorted(by_name.items()):
            result.relationships.extend(
                call_rows(
                    caller_label, caller_name, name, pins, bare, min(classes, default=None), file_path, repo_id,
                    no_self=True,  # every bare Python call site is `x.m()` on an untyped receiver
                    caller_owner=caller_owner,
                )
            )

    merge_same_nodes(result)
    add_heir_keys(result, _class_declarations(root, source_bytes), c3=True)
    return own_edges(result, file_path)


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract Python file and upsert results into the graph.

    This is a thin wrapper that calls extract_python_file() and then
    upserts each node and relationship via the GraphEngine.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the Python file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes), which is what makes multi-level relative imports
            resolve correctly and prevents same-named files in different
            directories from colliding into one Module node. When omitted
            (e.g. a caller indexing a standalone file with no repo context,
            as some tests do), falls back to the bare filename — matching
            this function's original behavior, so existing single-file
            callers keep working, just without the collision/relative-import
            benefits multi-file repos get from passing repo_root.

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
            module_name = file_path.name  # file_path wasn't actually under repo_root
    else:
        module_name = file_path.name

    result = extract_python_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
