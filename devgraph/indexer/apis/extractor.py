"""API extractor for parsing FastAPI, Flask, and Django route definitions."""

import ast
from dataclasses import dataclass, field
from typing import List

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from devgraph.indexer.apis.routes import MOUNT_SEP, join_python_route, join_route, mount_entry, unmounted_endpoint
from devgraph.indexer.common import member_owner
from devgraph.indexer.python.extractor import _extract_imports

_PY_LANGUAGE = Language(tspython.language())


@dataclass
class EndpointNode:
    """Represents an API Endpoint node. `mounted` False is a route on a
    router whose mount the file doesn't show (apis/routes.py
    `unmounted_endpoint`)."""

    path: str
    method: str
    repo_id: str
    properties: dict = field(default_factory=dict)
    mounted: bool = True

    @property
    def name(self) -> str:
        """The Endpoint's node name."""
        return f"{self.method} {self.path}" if self.mounted else unmounted_endpoint(
            self.method, self.path, self.properties["source"],
        )


@dataclass
class FunctionNode:
    """Represents a Function node (handler)."""

    name: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    """Represents a relationship between nodes.

    `to_file` pins the target: a file matches that file's node, "" the
    file-less handler stub, and None (Django) every node of the name.
    `to_owner` narrows a pinned file's nodes to the one of that owner (a
    module-level handler's "", a class-based view's class); None matches
    every one.
    """

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str
    properties: dict = field(default_factory=dict)
    to_file: str | None = None
    to_owner: str | None = None


@dataclass
class ExtractionResult:
    """Result of API extraction. `route_mounts` and `route_exports` are the
    file's Module properties of its routers another file mounts, as an
    Express file's are (apis/routes.py MOUNT_PROPERTIES), None when empty."""

    endpoints: List[EndpointNode] = field(default_factory=list)
    functions: List[FunctionNode] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)
    route_mounts: list[str] | None = None
    route_exports: list[str] | None = None


def _pinned_implements(
    endpoint_id: str, handler_name: str, filename: str, owner: str | None = None
) -> list[Relationship]:
    """A FastAPI/Flask Endpoint's IMPLEMENTS edges: the handler `def` follows
    its decorator in the route's own file, so the target is that file's
    Function of the handler's `owner` (the declarations it is in) and the file-less
    handler stub, never a same-named Function in another file or a
    same-named method of another class."""
    return [
        Relationship(
            source_label="Endpoint",
            source_name=endpoint_id,
            relationship_type="IMPLEMENTS",
            target_label="Function",
            target_name=handler_name,
            to_file=to_file,
            to_owner=owner if to_file else None,
        )
        for to_file in (filename, "")
    ]


#: The HTTP methods a FastAPI (or Flask 2) app or router has a decorator of.
_METHOD_DECORATORS = frozenset({"get", "post", "put", "delete", "patch", "options", "head"})
#: The calls making a FastAPI or a Flask app or router, by their last name.
_FACTORIES = {"FastAPI": "FastAPI", "APIRouter": "FastAPI", "Flask": "Flask", "Blueprint": "Flask"}
#: Receivers read as an app or router without a binding the file shows.
_ROUTER_NAMES = frozenset({"app", "router"})
#: A router factory's keyword giving its own prefix (an app has none: FastAPI's `root_path` is no route prefix).
_OWN_PREFIX = {"APIRouter": "prefix", "Blueprint": "url_prefix"}
#: The calls mounting a router on an app or router: (framework, the keyword giving the mount's prefix).
_MOUNT_CALLS = {"include_router": ("FastAPI", "prefix"), "register_blueprint": ("Flask", "url_prefix")}
#: The calls adding a route to an app or router: Flask's `add_url_rule(rule,
#: endpoint, view_func)` and FastAPI's `add_api_route(path, endpoint)`, with
#: (framework, the path's keyword, the view's keyword, the view's position).
_RULE_CALLS = {
    "add_url_rule": ("Flask", "rule", "view_func", 2), "add_api_route": ("FastAPI", "path", "endpoint", 1),
}
#: A Flask class-based view's bases that give it no HTTP method handler.
_VIEW_BASES = frozenset({"MethodView", "View", "object"})
#: The suffix of a Flask blueprint's export name for a mount that replaces
#: its own `url_prefix` (`register_blueprint(bp, url_prefix=...)`): Flask
#: puts the blueprint's routes under that prefix instead of its own, where
#: FastAPI joins `include_router`'s prefix to the router's.
REPLACED = ":replaced"


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _literal(node: Node | None, source: bytes) -> str | None:
    """A string literal's value, else None: a plain or raw string, or
    several written side by side (`"/a" "/b"`), escape sequences decoded
    outside a raw string; never an f-string with an interpolation."""
    if node is not None and node.type == "concatenated_string":
        parts = [_literal(child, source) for child in node.named_children]
        return None if any(part is None for part in parts) else "".join(parts)
    if node is None or node.type != "string" or any(c.type == "interpolation" for c in node.named_children):
        return None
    start = next((c for c in node.children if c.type == "string_start"), None)
    raw = start is not None and "r" in _text(start, source).lower()
    value = []
    for content in (c for c in node.named_children if c.type == "string_content"):
        at = content.start_byte
        for escape in (c for c in content.named_children if c.type == "escape_sequence"):
            value.append(source[at : escape.start_byte].decode("utf-8"))
            text = _text(escape, source)
            value.append(text if raw else _unescape(text))
            at = escape.end_byte
        value.append(source[at : content.end_byte].decode("utf-8"))
    return "".join(value)


def _unescape(escape: str) -> str:
    """One escape sequence's character (`\\x2F` is "/")."""
    try:
        return ast.literal_eval(f'"{escape}"')
    except (SyntaxError, ValueError):
        return escape


def _arguments(call: Node) -> tuple[list[Node], dict[str, Node]]:
    """A call's positional arguments and its keyword arguments by name."""
    positional: list[Node] = []
    keywords: dict[str, Node] = {}
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        if arg.type == "keyword_argument":
            name, value = arg.child_by_field_name("name"), arg.child_by_field_name("value")
            if name is not None and value is not None:
                keywords[name.text.decode("utf-8")] = value
        elif arg.type != "comment":
            positional.append(arg)
    return positional, keywords


def _methods(value: Node | None, source: bytes) -> list[str]:
    """Flask's `methods=`: a list, tuple or set of literal strings, or one
    comma-separated string; GET when not given or not literal."""
    if value is None:
        return ["GET"]
    items = value.named_children if value.type in ("list", "tuple", "set") else [value]
    found = [part.strip().upper() for item in items if (text := _literal(item, source)) for part in text.split(",")]
    return list(dict.fromkeys(each for each in found if each)) or ["GET"]


class _Routes:
    """One Python file's routes, read from its syntax tree: decorators,
    `add_url_rule`/`add_api_route` and URL-pattern calls only, never a
    comment or a string."""

    def __init__(self, root: Node, source: bytes):
        self.source = source
        self.bindings: dict[str, str] = {}  # app or router variable -> its framework
        self.apps: set[str] = set()  # the app variables (`FastAPI()`, `Flask()`)
        self.own: dict[str, str | None] = {}  # router variable -> its own prefix, None when not a literal
        # router variable -> where it is bound: "" the module's top level, a
        # top-level function's name, None anywhere else
        self.scope: dict[str, str | None] = {}
        self.returns: dict[str, set[str]] = {}  # top-level function -> the names it returns
        self.imports: set[str] = set()  # "FastAPI", "Flask": the frameworks the file imports
        self.decorated: list[tuple[Node, str, str]] = []  # (decorator call, handler, handler owner)
        self.calls: list[Node] = []
        self.call_scopes: list[str] = []  # per call, the owner a definition beside it has
        self.local: dict[str, list[tuple[str, str, str | None]]] = {}  # router -> its (mounted on, framework, prefix)
        self.classes: set[str] = set()  # the file's top-level classes
        self.functions: set[tuple[str, str]] = set()  # the file's functions: (name, owner)
        self.class_methods: dict[str, set[str]] = {}  # top-level class -> the methods it defines
        self.class_bases: dict[str, list[str]] = {}  # top-level class -> its bases as written
        # Iterative, in source order: a deeply nested expression (3,000 `+`
        # terms) is read on any thread's stack.
        pending: list[tuple[Node, str, str | None, str]] = [(root, "Module", None, "")]
        while pending:
            node, label, name, owner = pending.pop()
            inner = self._visit(node, label, name, owner)
            pending += [(child, *inner) for child in reversed(node.children)]

    def _visit(self, node: Node, label: str, name: str | None, owner: str) -> tuple[str, str | None, str]:
        """Read one node; return the (label, name, owner) its children are in."""
        source = self.source
        if node.type in ("import_statement", "import_from_statement"):
            for child in node.named_children:
                text = _text(child, source)
                for module, framework in (("fastapi", "FastAPI"), ("flask", "Flask")):
                    if child.type in ("dotted_name", "aliased_import") and (
                        text == module or text.startswith((module + ".", module + " "))
                    ):
                        self.imports.add(framework)
        elif node.type == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            called = right.child_by_field_name("function") if right is not None and right.type == "call" else None
            if left is not None and called is not None:
                factory = _text(called, source).rsplit(".", 1)[-1]
                framework = _FACTORIES.get(factory)
                if framework is not None:
                    target = _text(left, source)
                    self.bindings[target] = framework
                    self.scope[target] = "" if label == "Module" else (
                        name if label == "Function" and not owner else None
                    )
                    if factory in _OWN_PREFIX:
                        given = _arguments(right)[1].get(_OWN_PREFIX[factory])
                        self.own[target] = "" if given is None else _literal(given, source)
                        self.apps.discard(target)
                    else:
                        self.apps.add(target)
                        self.own.pop(target, None)
        elif node.type == "call":
            self.calls.append(node)
            self.call_scopes.append(member_owner(label, name, owner))
        elif node.type == "return_statement" and label == "Function" and not owner and name is not None:
            value = node.named_children[0] if node.named_children else None
            if value is not None and value.type == "identifier":
                self.returns.setdefault(name, set()).add(_text(value, source))
        elif node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            handler = definition.child_by_field_name("name") if definition is not None else None
            if definition is not None and definition.type == "function_definition" and handler is not None:
                for decorator in node.named_children:
                    target = decorator.named_children[0] if decorator.type == "decorator" and (
                        decorator.named_children) else None
                    if target is not None and target.type == "call":
                        self.decorated.append((target, _text(handler, source), member_owner(label, name, owner)))
        if node.type in ("class_definition", "function_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is not None and node.type == "class_definition" and label == "Module":
                self.classes.add(_text(name_node, source))
                bases = node.child_by_field_name("superclasses")
                self.class_bases[_text(name_node, source)] = [
                    _text(base, source) for base in (bases.named_children if bases is not None else [])
                    if base.type in ("identifier", "attribute")
                ]
            elif name_node is not None and node.type == "function_definition":
                self.functions.add((_text(name_node, source), member_owner(label, name, owner)))
                if label == "Class" and not owner and name in self.classes:
                    self.class_methods.setdefault(name, set()).add(_text(name_node, source))
            if name_node is not None:
                owner = member_owner(label, name, owner)
                label = "Class" if node.type == "class_definition" else "Function"
                name = _text(name_node, source)
        return label, name, owner

    def _framework(self, receiver: str, attr: str) -> str | None:
        """The framework of an app or router `receiver`'s `attr` decorator:
        the one its binding makes, else the one the file imports, else
        Flask's for `route` and FastAPI's for a method's; None for a
        receiver the file shows no app or router of."""
        if receiver in self.bindings:
            return self.bindings[receiver]
        if receiver not in _ROUTER_NAMES:
            return None
        if len(self.imports) == 1:
            return next(iter(self.imports))
        return "Flask" if attr == "route" else "FastAPI"

    def decorator_routes(self) -> list[tuple[str, str, str, str, str, str]]:
        """(framework, method, path, handler, handler owner, receiver) per
        route a decorator makes: `@app.get("/x")` its method,
        `@app.route("/x", methods=[...])` and FastAPI's `api_route` each
        method listed. The path is the decorator's own: `prefix` gives the
        receiver's."""
        found = []
        for call, handler, owner in self.decorated:
            func = call.child_by_field_name("function")
            if func is None or func.type != "attribute":
                continue
            receiver, attr = func.child_by_field_name("object"), func.child_by_field_name("attribute")
            if receiver is None or attr is None:
                continue
            attr_name = _text(attr, self.source)
            if attr_name not in _METHOD_DECORATORS and attr_name not in ("route", "api_route"):
                continue
            receiver_text = _text(receiver, self.source)
            framework = self._framework(receiver_text, attr_name)
            positional, keywords = _arguments(call)
            path = _literal(positional[0] if positional else keywords.get("path") or keywords.get("rule"), self.source)
            if framework is None or path is None:
                continue
            methods = [attr_name.upper()] if attr_name in _METHOD_DECORATORS else _methods(
                keywords.get("methods"), self.source,
            )
            found += [(framework, method, path, handler, owner, receiver_text) for method in methods]
        return found

    def rule_calls(self) -> list[tuple[str, list[str] | None, str, Node, str, str]]:
        """(framework, its methods, path, view, receiver, the call's scope:
        the owner a definition beside it has) per `add_url_rule`
        and `add_api_route` call on an app or router (`_routing`) whose path
        is a literal and that gives a view; the methods are `methods=`'s
        (`_methods`), None when not given."""
        found = []
        for call, scope in zip(self.calls, self.call_scopes):
            func = call.child_by_field_name("function")
            if func is None or func.type != "attribute":
                continue
            obj, attr = func.child_by_field_name("object"), func.child_by_field_name("attribute")
            if obj is None or attr is None or _text(attr, self.source) not in _RULE_CALLS:
                continue
            framework, path_keyword, view_keyword, at = _RULE_CALLS[_text(attr, self.source)]
            receiver = _text(obj, self.source)
            positional, keywords = _arguments(call)
            path = _literal(positional[0] if positional else keywords.get(path_keyword), self.source)
            view = keywords.get(view_keyword) or (positional[at] if len(positional) > at else None)
            if path is None or view is None or not self._routing(receiver):
                continue
            given = keywords.get("methods")
            found.append((
                framework, None if given is None else _methods(given, self.source), path, view, receiver, scope,
            ))
        return found

    def view_methods(self, cls: str, seen: frozenset = frozenset()) -> tuple[str, ...] | None:
        """The HTTP method handlers (`get`, `post`, ...) a top-level class
        of the file defines or inherits from classes of the file, as a
        Flask `MethodView` routes them; None when it is no class of the
        file or a base of it is another file's (its methods are not known
        here)."""
        if cls not in self.classes or cls in seen:
            return None
        found = self.class_methods.get(cls, set()) & set(_HTTP_METHOD_HANDLERS)
        for base in self.class_bases.get(cls, ()):
            if base.rsplit(".", 1)[-1] in _VIEW_BASES:
                continue
            inherited = self.view_methods(base, seen | {cls})
            if inherited is None:
                return None
            found |= set(inherited)
        return tuple(method for method in _HTTP_METHOD_HANDLERS if method in found)

    def mount_calls(self) -> list[tuple[str, str, str | None, Node]]:
        """(framework, the app or router mounted on, the prefix, the router
        mounted) per `include_router(router, prefix=...)` and
        `register_blueprint(bp, url_prefix=...)` call on an app or router,
        the prefix None when not given; one whose prefix is not a literal
        is none."""
        found = []
        for call in self.calls:
            func = call.child_by_field_name("function")
            if func is None or func.type != "attribute":
                continue
            obj, attr = func.child_by_field_name("object"), func.child_by_field_name("attribute")
            if obj is None or attr is None or _text(attr, self.source) not in _MOUNT_CALLS:
                continue
            framework, keyword = _MOUNT_CALLS[_text(attr, self.source)]
            positional, keywords = _arguments(call)
            router = positional[0] if positional else keywords.get("router" if framework == "FastAPI" else "blueprint")
            given = keywords.get(keyword)
            prefix = None if given is None else _literal(given, self.source)
            parent = _text(obj, self.source)
            if router is None or (given is not None and prefix is None) or not self._routing(parent):
                continue
            found.append((framework, parent, prefix, router))
        return found

    def _routing(self, receiver: str) -> bool:
        """Whether a receiver is an app or router (`_framework`)."""
        return receiver in self.bindings or receiver in _ROUTER_NAMES

    def _is_app(self, receiver: str) -> bool:
        return receiver in self.apps or (receiver == "app" and receiver not in self.bindings)

    def prefixes(self, receiver: str, seen: frozenset = frozenset()) -> set[str]:
        """The full prefixes of an app's or router's routes: "" for an
        app's, a router's own joined to those of each chain of mounts the
        file shows (`local`) up to an app; none when the file mounts it on
        no app, or its own prefix is not a literal (its routes are then
        under none, and no mount renames them: `exports` gives none)."""
        if self._is_app(receiver):
            return {""}
        own = self.own.get(receiver, "")
        if own is None or receiver in seen:
            return set()
        found = set()
        for parent, framework, mounted in self.local.get(receiver, ()):
            for above in self.prefixes(parent, seen | {receiver}):
                if framework == "Flask":
                    found.add(join_route(above, own if mounted is None else mounted))
                else:
                    found.add(join_route(above, mounted or "", own))
        return found

    def factories(self, function: str) -> list[str]:
        """The router variables a top-level `function` returns: one it binds,
        or a top-level one."""
        return sorted(
            receiver for receiver in self.returns.get(function, ())
            if receiver in self.own and self.scope.get(receiver) in (function, "")
        )

    def exports(self, receiver: str) -> list[tuple[str, str]]:
        """(export name, the prefix its routes are under) of a router
        another module can import and mount: a top-level variable's name, and
        `f()` for a top-level function `f` that returns it (`factories`), each
        with its own prefix, and a Flask blueprint's each also with
        REPLACED and none."""
        own = self.own.get(receiver)
        if own is None or not receiver.isidentifier():
            return []
        names = [receiver] if self.scope.get(receiver) == "" else []
        names += [f"{function}()" for function in sorted(self.returns) if receiver in self.factories(function)]
        if self.bindings[receiver] == "Flask":
            return [variant for each in names for variant in ((each, own), (each + REPLACED, ""))]
        return [(each, own) for each in names]

    def django_routes(self) -> list[tuple[str, str, str, tuple[str, ...] | None]]:
        """(pattern, view, the view as written, its methods) per Django
        `path(...)`/`re_path(...)` call whose pattern is a literal and whose
        view is a name (`views.search` is `search`, methods None) or a
        class-based view's `as_view()` (the class): its methods are the
        names of the class's methods the route can call, Django's HTTP
        method handlers (`get`, `post`, ...), or for a DRF ViewSet's
        `as_view({"get": "list"})` the actions its literal mapping names."""
        found = []
        for call in self.calls:
            func = call.child_by_field_name("function")
            if func is None or func.type not in ("identifier", "attribute"):
                continue
            if _text(func, self.source).rsplit(".", 1)[-1] not in ("path", "re_path"):
                continue
            positional, _keywords = _arguments(call)
            pattern = _literal(positional[0], self.source) if len(positional) >= 2 else None
            view = positional[1] if len(positional) >= 2 else None
            methods = None
            if view is not None and view.type == "call":  # `ArticleView.as_view()`
                inner = view.child_by_field_name("function")
                obj = inner.child_by_field_name("object") if inner is not None and inner.type == "attribute" else None
                as_view = inner is not None and _text(inner, self.source).endswith(".as_view")
                methods = self._view_methods(view) if as_view else None
                view = obj if as_view else None
            if pattern is None or view is None or view.type not in ("identifier", "attribute"):
                continue
            written = _text(view, self.source)
            found.append((pattern, written.rsplit(".", 1)[-1], written, methods))
        return found

    def _view_methods(self, as_view: Node) -> tuple[str, ...]:
        """The methods an `as_view(...)` call routes to: a DRF ViewSet's
        literal `{"get": "list", ...}` mapping's actions, else Django's
        HTTP method handlers."""
        positional, keywords = _arguments(as_view)
        mapping = positional[0] if positional else keywords.get("actions")
        if mapping is not None and mapping.type == "dictionary":
            actions = [
                _literal(pair.child_by_field_name("value"), self.source)
                for pair in mapping.named_children if pair.type == "pair"
            ]
            return tuple(dict.fromkeys(action for action in actions if action))
        return _HTTP_METHOD_HANDLERS


def _imported_router(node: Node, source: bytes, bindings) -> tuple[str, list[str]] | None:
    """(export, the files it can be in) of a router another module defines,
    as `include_router`/`register_blueprint` names it: a from-imported name
    (`from .users import router`) the module's name, `users.router` of an
    imported module (`from . import users`, `import app.users as users`)
    its `router`, and a call of such a function (`build_router(engine)`)
    `f()`, as the file defining it exports the router it returns
    (`_Routes.exports`); None when no import names it."""
    if node.type == "call":
        func = node.child_by_field_name("function")
        target = _imported_router(func, source, bindings) if func is not None else None
        return None if target is None else (f"{target[0]}()", target[1])
    text = _text(node, source)
    if node.type == "identifier" and (symbol := bindings.symbols.get(text)) is not None:
        return symbol.name, list(dict.fromkeys(symbol.module.files()))
    if node.type == "attribute":
        receiver, export = text.rsplit(".", 1)
        if (module := bindings.modules.get(receiver)) is not None:
            return export, list(dict.fromkeys(module.files()))
        if (symbol := bindings.symbols.get(receiver)) is not None:
            return export, list(dict.fromkeys(symbol.submodule.files()))
    return None


#: The methods of a Django (or DRF `APIView`) class-based view that handle
#: a request, by its method: `View.http_method_names`.
_HTTP_METHOD_HANDLERS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


def _django_implements(
    endpoint_id: str, view: str, written: str, bindings, methods: tuple[str, ...] | None = None,
    filename: str = "", classes: frozenset[str] | set[str] = frozenset(),
) -> tuple[str, list[Relationship]]:
    """A Django route's view name and IMPLEMENTS edges: the file-less handler
    stub (named as the view is where it is defined: a from-import's alias
    is the name it imports), and the
    view in the module it is imported from, as a top-level function of
    that module's candidate files (python/resolve.py): `views.search` with
    `from . import views` is `<dir>/views.py`'s `search`, a from-imported
    `search` its module's. A class-based view (`methods` not None) is the
    top-level Class there instead, with its `methods` there (owner the
    class) that it defines itself or inherits from a class of its file; a
    bare class-based view nothing imports that is a top-level class of the
    route's own `filename` (one of its `classes`) is that file's. A view
    read off a module nothing imports links nothing but the stub; any other
    bare name nothing imports (a star import's, a function of the file
    itself) links every function (or class) of its name, as before, and
    no method."""
    head = written.split(".")[0] if "." in written else None
    label = "Function" if methods is None else "Class"
    pins: list[str] | None = None
    if head is not None:
        receiver = written.rsplit(".", 1)[0] if written.count(".") == 1 else None
        module = bindings.modules.get(receiver) if receiver is not None else None
        symbol = bindings.symbols.get(receiver) if receiver is not None else None
        if module is not None:
            pins = module.files()
        elif symbol is not None:
            pins = symbol.submodule.files()  # `from app import views`: the module app/views
        else:
            pins = []  # unresolved: the stub alone
    elif (symbol := bindings.symbols.get(written)) is not None:
        view, pins = symbol.name, symbol.module.files()
    elif methods is not None and written in classes:
        pins = [filename]

    def implements(target_label, target_name, to_file=None, to_owner=None):
        return Relationship(
            source_label="Endpoint", source_name=endpoint_id, relationship_type="IMPLEMENTS",
            target_label=target_label, target_name=target_name, to_file=to_file, to_owner=to_owner,
        )

    if pins is None:
        if methods is None:
            return view, [implements("Function", view)]
        return view, [implements("Function", view, ""), implements("Class", view)]
    pins = list(dict.fromkeys(pins))
    return view, [implements("Function", view, ""), *(implements(label, view, pin, "") for pin in pins)] + [
        implements("Function", method, pin, view) for pin in pins for method in methods or ()
    ]


def _rule_views(
    view: Node, methods: list[str] | None, routes: _Routes, bindings, filename: str, source: bytes, scope: str,
):
    """(method, handler, its IMPLEMENTS edges from an Endpoint) per route of
    an `add_url_rule`/`add_api_route` view (`_Routes.rule_calls`).

    A function of the file the call's scope sees (one defined beside the
    call, an app factory's nested `index`, else in a scope around it) is
    that file's of its owner, as a decorated handler is; any
    other name links where `_django_implements` finds it (its imported
    module's). A class-based view's `as_view(...)` (Flask's `MethodView`)
    is its Class and, per method, the method's handler (`get` for GET);
    with no `methods=` its methods are the HTTP method handlers the class
    has (`_Routes.view_methods`), or for a class whose methods aren't
    known here (another file's) one "*" route linking every HTTP method
    handler of it, as a Django class-based view's does. Any other view
    (a lambda) is no route."""
    class_view = False
    if view.type == "call":  # `UserAPI.as_view("user_api")`
        inner = view.child_by_field_name("function")
        view = inner.child_by_field_name("object") if inner is not None and inner.type == "attribute" else None
        if view is None or not _text(inner, source).endswith(".as_view"):
            return []
        if methods is None:
            known = routes.view_methods(_text(view, source))
            methods = [method.upper() for method in known] if known else ["*"]
        class_view = True
    if view.type not in ("identifier", "attribute"):
        return []
    written = _text(view, source)
    scopes = [scope.rsplit(".", depth)[0] for depth in range(scope.count(".") + 1)] + [""] if scope else [""]
    owner = next((each for each in scopes if (written, each) in routes.functions), None)
    if not class_view and view.type == "identifier" and owner is not None:
        return [
            (method, written, lambda endpoint: _pinned_implements(endpoint, written, filename, owner))
            for method in methods or ["GET"]
        ]

    def link(endpoint: str, method: str) -> tuple[str, list[Relationship]]:
        linked = None if not class_view else _HTTP_METHOD_HANDLERS if method == "*" else (method.lower(),)
        return _django_implements(
            endpoint, written.rsplit(".", 1)[-1], written, bindings, linked, filename, routes.classes,
        )

    name = link("", "*")[0]
    return [
        (method, name, lambda endpoint, method=method: link(endpoint, method)[1]) for method in methods or ["GET"]
    ]


class APIExtractor:
    """Extracts API endpoints and their handlers from Python source code."""

    def __init__(self, repo_id: str):
        """Initialize the API extractor.

        Args:
            repo_id: The repository identifier for scoping extracted nodes.
        """
        self.repo_id = repo_id

    def extract_from_source(
        self, content: str, filename: str = "unknown.py", roots: tuple[str, ...] = (),
    ) -> ExtractionResult:
        """Extract API endpoints from Python source code.

        Supports FastAPI, Flask, and Django patterns, read from the file's
        syntax tree, so a route in a comment or a docstring is none. A route
        decorator is FastAPI's or Flask's by the app or router it is on (the
        call that made it, else the framework the file imports): a FastAPI
        `@app.post` is POST alone, and Flask's `methods=[...]` gives each
        method listed. Flask's `add_url_rule` and FastAPI's `add_api_route`
        calls on an app or router are routes too (`_rule_views`).

        A router's routes are under its own literal prefix (`APIRouter(
        prefix=...)`, `Blueprint(url_prefix=...)`) and those of the mounts
        the file shows up to an app (`include_router(router, prefix=...)`,
        joined to the router's; `register_blueprint(bp, url_prefix=...)`,
        replacing the blueprint's). A router the file mounts on no app is
        mounted where the file doesn't show (`unmounted_endpoint`); one
        another module can import is recorded in `route_exports`, and the
        file's mounts of another module's routers in `route_mounts`, for
        the dispatcher to name by the mounts that reach them, as an Express
        router is (apis/routes.py MountTable).

        Args:
            content: The Python source code content.
            filename: The source filename for context.
            roots: The file's configured Python source roots
                (`ResolverConfig.python_roots`), which a Django view's
                module and a mounted router's are also looked up under, as
                the Python extractor's imports are.

        Returns:
            ExtractionResult containing extracted Endpoint and Function nodes.
        """
        result = ExtractionResult()
        source = content.encode("utf-8")
        root = Parser(_PY_LANGUAGE).parse(source).root_node
        routes = _Routes(root, source)
        foreign = []
        for framework, parent, prefix, router in routes.mount_calls():
            func = router.child_by_field_name("function") if router.type == "call" else None
            if router.type == "identifier" and _text(router, source) in routes.own:
                routes.local.setdefault(_text(router, source), []).append((parent, framework, prefix))
            elif func is not None and func.type == "identifier" and routes.factories(_text(func, source)):
                # `include_router(build_router(...))` of the file's own function.
                for made in routes.factories(_text(func, source)):
                    routes.local.setdefault(made, []).append((parent, framework, prefix))
            else:
                foreign.append((framework, parent, prefix, router))
        django = routes.django_routes()
        rules = routes.rule_calls()
        bindings = _extract_imports(root, source, filename, roots) if django or foreign or rules else None
        # (framework, method, path, receiver, handler, its IMPLEMENTS edges from an Endpoint)
        found = [
            (framework, method, path, receiver, handler,
             lambda endpoint, handler=handler, owner=owner: _pinned_implements(endpoint, handler, filename, owner))
            for framework, method, path, handler, owner, receiver in routes.decorator_routes()
        ]
        for framework, methods, path, view, receiver, scope in rules:
            found += [
                (framework, method, path, receiver, handler, link)
                for method, handler, link in _rule_views(view, methods, routes, bindings, filename, source, scope)
            ]
        # A route on a router the file mounts on no app is the file's own
        # (`unmounted_endpoint`); one of a router another module can import
        # is also a `route_exports` entry, unless another unmounted route of
        # the file shares its Endpoint.
        deferred: dict[str, set[str]] = {}
        kept: set[str] = set()
        for framework, method, path, receiver, handler, link in found:
            aboves = routes.prefixes(receiver)
            fulls = sorted(path if above == "" else join_python_route(above, path) for above in aboves)
            if not aboves:
                # Mounted where the file doesn't show: under its own prefix (none when not a literal).
                # With neither, `""` stays "" (`GET ? (file)`), apart from `"/"`: under a mount's
                # "/items" one is "/items", the other "/items/".
                under_own = routes.own.get(receiver) or ""
                full = join_python_route(under_own, path) if under_own or path else ""
                own = unmounted_endpoint(method, full, filename)
                if exports := routes.exports(receiver):
                    deferred.setdefault(own, set()).update(
                        MOUNT_SEP.join((name, method, exported, *([full] if exported != full else [])))
                        for name, under in exports
                        # A bare "" stays one: under "/api" it is "/api", where "/" is "/api/".
                        for exported in ["" if not under.strip("/") and not path else join_python_route(under, path)]
                    )
                else:
                    kept.add(own)
                fulls = [full]
            for full in fulls:
                endpoint = self._add(result, framework, method, full, handler, filename, "handler", bool(aboves))
                result.relationships.extend(link(endpoint))
        result.route_exports = sorted(
            entry for own, entries in deferred.items() if own not in kept for entry in entries
        ) or None
        route_mounts = set()
        for framework, parent, prefix, router in foreign:
            target = _imported_router(router, source, bindings)
            if target is None:
                continue
            export, files = target
            if framework == "Flask" and prefix is not None:
                export += REPLACED
            aboves = routes.prefixes(parent)
            route_mounts |= {mount_entry("", join_route(above, prefix or ""), export, files) for above in aboves}
            if not aboves:
                route_mounts |= {
                    mount_entry(name, join_route(under, prefix or ""), export, files)
                    for name, under in routes.exports(parent)
                }
        result.route_mounts = sorted(route_mounts) or None
        for pattern, view, written, methods in django:
            view, implements = _django_implements(
                f"* {pattern}", view, written, bindings, methods, filename, routes.classes,
            )
            # Django's urls.py names no HTTP method (the view handles each).
            self._add(result, "Django", "*", pattern, view, filename, "view")
            result.relationships += implements
        return result

    def _add(
        self, result: ExtractionResult, framework: str, method: str, path: str, handler: str, filename: str,
        kind: str, mounted: bool = True,
    ) -> str:
        """One route's Endpoint and its handler stub; returns the Endpoint's name."""
        endpoint = EndpointNode(
            path=path, method=method, repo_id=self.repo_id, properties={"framework": framework, "source": filename},
            mounted=mounted,
        )
        result.endpoints.append(endpoint)
        result.functions.append(FunctionNode(
            name=handler, repo_id=self.repo_id, properties={"type": kind, "source": filename},
        ))
        return endpoint.name
