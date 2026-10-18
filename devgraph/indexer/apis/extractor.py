"""API extractor for parsing FastAPI, Flask, and Django route definitions."""

from dataclasses import dataclass, field
from typing import List

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import member_owner

_PY_LANGUAGE = Language(tspython.language())


@dataclass
class EndpointNode:
    """Represents an API Endpoint node."""

    path: str
    method: str
    repo_id: str
    properties: dict = field(default_factory=dict)


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
    """Result of API extraction."""

    endpoints: List[EndpointNode] = field(default_factory=list)
    functions: List[FunctionNode] = field(default_factory=list)
    relationships: List[Relationship] = field(default_factory=list)


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


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _literal(node: Node | None, source: bytes) -> str | None:
    """A plain string literal's text (no f-string interpolation), else None."""
    if node is None or node.type != "string" or any(c.type == "interpolation" for c in node.named_children):
        return None
    return "".join(_text(c, source) for c in node.named_children if c.type == "string_content")


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
    """One Python file's routes, read from its syntax tree: decorators and
    URL-pattern calls only, never a comment or a string."""

    def __init__(self, root: Node, source: bytes):
        self.source = source
        self.bindings: dict[str, str] = {}  # app or router variable -> its framework
        self.imports: set[str] = set()  # "FastAPI", "Flask": the frameworks the file imports
        self.decorated: list[tuple[Node, str, str]] = []  # (decorator call, handler, handler owner)
        self.calls: list[Node] = []
        self._walk(root, "Module", None, "")

    def _walk(self, node: Node, label: str, name: str | None, owner: str) -> None:
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
                framework = _FACTORIES.get(_text(called, source).rsplit(".", 1)[-1])
                if framework is not None:
                    self.bindings[_text(left, source)] = framework
        elif node.type == "call":
            self.calls.append(node)
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
            if name_node is not None:
                owner = member_owner(label, name, owner)
                label = "Class" if node.type == "class_definition" else "Function"
                name = _text(name_node, source)
        for child in node.children:
            self._walk(child, label, name, owner)

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

    def decorator_routes(self) -> list[tuple[str, str, str, str, str]]:
        """(framework, method, path, handler, handler owner) per route a
        decorator makes: `@app.get("/x")` its method, `@app.route("/x",
        methods=[...])` and FastAPI's `api_route` each method listed."""
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
            framework = self._framework(_text(receiver, self.source), attr_name)
            positional, keywords = _arguments(call)
            path = _literal(positional[0] if positional else keywords.get("path") or keywords.get("rule"), self.source)
            if framework is None or path is None:
                continue
            methods = [attr_name.upper()] if attr_name in _METHOD_DECORATORS else _methods(
                keywords.get("methods"), self.source,
            )
            found += [(framework, method, path, handler, owner) for method in methods]
        return found

    def django_routes(self) -> list[tuple[str, str]]:
        """(pattern, view) per Django `path(...)`/`re_path(...)` call whose
        pattern is a literal and whose view is a name (`views.search` is
        `search`) or a class-based view's `as_view()` (the class)."""
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
            if view is not None and view.type == "call":  # `ArticleView.as_view()`
                inner = view.child_by_field_name("function")
                obj = inner.child_by_field_name("object") if inner is not None and inner.type == "attribute" else None
                view = obj if inner is not None and _text(inner, self.source).endswith(".as_view") else None
            if pattern is None or view is None or view.type not in ("identifier", "attribute"):
                continue
            found.append((pattern, _text(view, self.source).rsplit(".", 1)[-1]))
        return found


class APIExtractor:
    """Extracts API endpoints and their handlers from Python source code."""

    def __init__(self, repo_id: str):
        """Initialize the API extractor.

        Args:
            repo_id: The repository identifier for scoping extracted nodes.
        """
        self.repo_id = repo_id

    def extract_from_source(self, content: str, filename: str = "unknown.py") -> ExtractionResult:
        """Extract API endpoints from Python source code.

        Supports FastAPI, Flask, and Django patterns, read from the file's
        syntax tree, so a route in a comment or a docstring is none. A route
        decorator is FastAPI's or Flask's by the app or router it is on (the
        call that made it, else the framework the file imports): a FastAPI
        `@app.post` is POST alone, and Flask's `methods=[...]` gives each
        method listed.

        Args:
            content: The Python source code content.
            filename: The source filename for context.

        Returns:
            ExtractionResult containing extracted Endpoint and Function nodes.
        """
        result = ExtractionResult()
        source = content.encode("utf-8")
        routes = _Routes(Parser(_PY_LANGUAGE).parse(source).root_node, source)
        for framework, method, path, handler, owner in routes.decorator_routes():
            self._add(result, framework, method, path, handler, filename, "handler")
            result.relationships.extend(_pinned_implements(f"{method} {path}", handler, filename, owner))
        for pattern, view in routes.django_routes():
            # Django's urls.py names no HTTP method (the view handles each);
            # its view is usually imported from views.py, so the target
            # stays unpinned.
            self._add(result, "Django", "*", pattern, view, filename, "view")
            result.relationships.append(Relationship(
                source_label="Endpoint",
                source_name=f"* {pattern}",
                relationship_type="IMPLEMENTS",
                target_label="Function",
                target_name=view,
            ))
        return result

    def _add(
        self, result: ExtractionResult, framework: str, method: str, path: str, handler: str, filename: str,
        kind: str,
    ) -> None:
        """One route's Endpoint and its handler stub."""
        result.endpoints.append(EndpointNode(
            path=path, method=method, repo_id=self.repo_id, properties={"framework": framework, "source": filename},
        ))
        result.functions.append(FunctionNode(
            name=handler, repo_id=self.repo_id, properties={"type": kind, "source": filename},
        ))
