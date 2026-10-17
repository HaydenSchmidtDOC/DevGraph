"""Tree-sitter-based JavaScript/TypeScript source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape (Implementation Plan
#8, row 1: JS/TS), reusing the shared `GraphNode`/`GraphRelationship`/
`ExtractionResult` dataclasses from `devgraph.indexer.common` rather than
redefining them.

Parses a .js/.jsx/.mjs/.cjs/.ts/.tsx/.mts/.cts file and extracts:
  - Module (the file itself)
  - Classes (ES6 `class`), with `extends` -> EXTENDS (an `implements` clause
    on a TS class is intentionally NOT an EXTENDS edge - it's an interface
    conformance, not inheritance, and DevGraph has no Interface node label)
  - Functions: function declarations, function expressions and arrow
    functions assigned to a name (`const x = () => {}` / `const x =
    function() {}`), and class methods/field-initializer functions. Anonymous
    / inline arrows not bound to an identifier (callback arguments, IIFEs,
    etc.) are intentionally NOT extracted as Function nodes - see the
    module's "Known limitations" note below.
  - CONTAINS relationships (Module/Class -> Class/Function)
  - CALLS relationships, name-resolved the same way as the Python extractor:
    `foo()` -> 'foo', `obj.foo()`/`this.foo()` -> 'foo' (the member name).
    A JSX element rendering a component (`<Button />`, `<Ui.Button>`; not
    an intrinsic `<div>`) is a call of it, resolved like `Button()` but
    never by bare name alone.
  - IMPORTS relationships from ES module `import`/`export ... from` syntax
    and CommonJS `require()` calls.
  - JSDoc (`/** ... */`) comments immediately preceding a class/function as
    that node's `description`/`docstring_full` properties (module-level
    JSDoc is not extracted - out of scope, the brief only asks for
    class/function docstrings).

Grammar routing is purely by file extension: '.ts', '.mts' and '.cts' use
the TypeScript grammar, '.tsx' uses the TSX grammar (JSX + TS types), and
everything else ('.js', '.jsx', '.mjs', '.cjs', and any unrecognized
extension) uses the plain JavaScript grammar. One function (`extract_js_file`) handles every extension rather
than exposing one function per grammar, since the tree-walking logic
downstream of the parse is identical across all three grammars - only the
`Language` handed to the Tree-sitter `Parser` differs.

Known limitations (v1 scope cuts, documented per Implementation Plan #8):
  - Anonymous/inline arrow functions and function expressions not bound to
    an identifier (e.g. `arr.map(x => x + 1)`, an IIFE) are not extracted as
    Function nodes - no name to key a node on, and doing so would require a
    synthetic naming scheme this plan doesn't call for.
  - `implements` (TS interfaces) does not produce an EXTENDS edge, and
    `interface` declarations are not extracted as nodes at all - there is no
    Interface node label in the schema and the brief scopes this to Class/
    Function/CONTAINS/CALLS/IMPORTS/EXTENDS.
  - IMPORTS resolution: relative specifiers (`./x`, `../x`) are resolved
    against the importing file's directory with a same-repo-file-path guess
    across `.js/.jsx/.ts/.tsx` (plus `/index.<ext>` directory-import
    candidates), or, for an explicit `.js`/`.jsx`/`.mjs`/`.cjs` extension,
    across the TypeScript sources that emit it - the same non-materializing-guess pattern the Python
    extractor uses (a guessed target that doesn't match a real indexed file
    simply never produces an edge, since upsert_relationship only
    MATCH-links real existing nodes). A bare specifier the file's
    tsconfig.json/jsconfig.json maps (`paths`, `baseUrl`, through `extends`
    and `references`; see resolver_config.py) names the same candidates
    under the mapped path, and so does the name of a workspace package (the
    root package.json's `workspaces` or pnpm-workspace.yaml's `packages`)
    under that package's entry point or subpath; any other bare specifier
    (`import x from 'lodash'`) is an external package, with no IMPORTS edge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from devgraph.indexer.apis.routes import add_route, join_route
from devgraph.indexer.calls import STOP_METHODS, STOP_TYPES, call_rows
from devgraph.indexer.common import (
    ExtractionResult,
    GraphNode,
    GraphRelationship,
    add_heir_keys,
    class_member_pin,
    member_owner,
    own_edges,
    owned_pin,
    split_owned_pin,
)
from devgraph.indexer.resolver_config import ResolverConfig

logger = logging.getLogger(__name__)

_JS_LANGUAGE = Language(tsjs.language())
_TS_LANGUAGE = Language(tsts.language_typescript())
_TSX_LANGUAGE = Language(tsts.language_tsx())

_CLASS_TYPES = ("class_declaration", "abstract_class_declaration", "class_expression")
_FUNCTION_VALUE_TYPES = ("function_expression", "arrow_function", "generator_function")
_NESTED_SCOPE_TYPES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "arrow_function",
        "generator_function",
        "method_definition",
        "class_declaration",
        "abstract_class_declaration",
        "class_expression",
    }
)
_EXTENSIONS = ("js", "jsx", "ts", "tsx")
# An explicit extension in a specifier names the emitted file; TypeScript's
# NodeNext/Bundler resolution maps it back to the source that emits it.
_EXPLICIT_EXTENSIONS = {
    ".js": ("ts", "tsx", "js"),
    ".jsx": ("tsx", "jsx"),
    ".mjs": ("mts", "mjs"),
    ".cjs": ("cts", "cjs"),
    ".ts": ("ts",),
    ".tsx": ("tsx",),
    ".mts": ("mts",),
    ".cts": ("cts",),
}


def _make_parser(language: Language) -> Parser:
    return Parser(language)


def _language_for_extension(suffix: str) -> Language:
    suffix = suffix.lower()
    if suffix in (".ts", ".mts", ".cts"):
        return _TS_LANGUAGE
    if suffix == ".tsx":
        return _TSX_LANGUAGE
    return _JS_LANGUAGE  # '.js', '.jsx', '.mjs', '.cjs', and any unrecognized extension


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8")


def _dotted_name(node: Node, source: bytes) -> str:
    """Render an identifier/member-expression node (e.g. `a.b.c`) as dotted text."""
    if node.type in ("identifier", "property_identifier", "type_identifier", "this"):
        return _text(node, source)
    if node.type == "member_expression":
        obj = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if obj is not None and prop is not None:
            return f"{_dotted_name(obj, source)}.{_text(prop, source)}"
    if node.type == "nested_type_identifier":
        # TS namespaced type reference, e.g. `ns.Type`.
        left = node.child_by_field_name("module")
        right = node.child_by_field_name("name")
        if left is not None and right is not None:
            return f"{_dotted_name(left, source)}.{_text(right, source)}"
    return _text(node, source)


def _extract_jsdoc(anchor: Node, source: bytes) -> str | None:
    """Return a JSDoc comment's cleaned text if it immediately precedes `anchor`
    (its previous named sibling in the same parent block).
    """
    prev = anchor.prev_named_sibling
    if prev is None or prev.type != "comment":
        return None
    raw = _text(prev, source)
    if not raw.startswith("/**"):
        return None
    return _clean_jsdoc(raw)


def _clean_jsdoc(raw: str) -> str:
    """Strip `/** ... */` delimiters and each line's leading `*` decoration."""
    text = raw.strip()
    if text.startswith("/**"):
        text = text[3:]
    if text.endswith("*/"):
        text = text[:-2]

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("*"):
            line = line[1:]
            if line.startswith(" "):
                line = line[1:]
        lines.append(line)

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines).strip()


def _docstring_summary(full_text: str, max_chars: int = 120) -> str:
    """First line/sentence of a JSDoc block, truncated as a display summary."""
    first_para = full_text.split("\n\n", 1)[0].strip()
    first_line = first_para.split("\n", 1)[0].strip()

    period_idx = first_line.find(". ")
    if period_idx != -1:
        first_line = first_line[: period_idx + 1]

    if len(first_line) > max_chars:
        first_line = first_line[: max_chars - 3].rstrip() + "..."
    return first_line


#: A JSX element's opening tag: `<Button>` and `<Button />`.
_JSX_TAGS = ("jsx_opening_element", "jsx_self_closing_element")


def _jsx_component(node: Node) -> Node | None:
    """The name of a JSX element that renders a component, a call of it:
    a capitalised identifier (`<Button />`) or a member expression
    (`<Ui.Button>`); None for an intrinsic tag (`<div>`, `<svg:rect>`)."""
    name = node.child_by_field_name("name")
    if name is None:
        return None
    if name.type == "member_expression" or (name.type == "identifier" and name.text[:1].isupper()):
        return name
    return None


#: Calls that wrap a function component (`memo(fn)`, `React.forwardRef(fn)`):
#: `const Card = memo(() => ...)` is the component `Card`.
_COMPONENT_WRAPPERS = frozenset({"memo", "forwardRef"})


def _function_value(value: Node | None) -> Node | None:
    """The function a declarator's value defines, the node a Function is
    made of: a function or arrow; the one a component wrapper takes
    (`memo(...)`, `forwardRef(...)`, nested); a styled component
    (`styled.div`...``, `styled(Button)`...``), which has no body. None for
    anything else."""
    if value is None:
        return None
    if value.type in _FUNCTION_VALUE_TYPES:
        return value
    if value.type != "call_expression":
        return None
    func, args = value.child_by_field_name("function"), value.child_by_field_name("arguments")
    if func is None or args is None:
        return None
    if args.type == "template_string":
        callee = func  # `styled.div`, `styled(Button)`, `styled.div.attrs({})`, down to `styled`
        while callee is not None and callee.type in ("member_expression", "call_expression"):
            callee = callee.child_by_field_name("object" if callee.type == "member_expression" else "function")
        return value if callee is not None and callee.type == "identifier" and callee.text == b"styled" else None
    name = func.text.rsplit(b".", 1)[-1].decode("utf-8", errors="replace")
    if name in _COMPONENT_WRAPPERS and args.named_children:
        return _function_value(args.named_children[0])
    return None


def _named_function(node: Node) -> bool:
    """Whether the function or arrow `node` is a Function of its own: a
    declarator's (`_function_value`) or a class field's value."""
    parent = node.parent
    while parent is not None and parent.type in ("arguments", "call_expression", "parenthesized_expression"):
        parent = parent.parent
    if parent is None:
        return False
    if parent.type in ("field_definition", "public_field_definition"):
        return True
    if parent.type != "variable_declarator":
        return False
    defined = _function_value(parent.child_by_field_name("value"))
    return defined is not None and (defined.start_byte, defined.end_byte) == (node.start_byte, node.end_byte)


def _bound_names(func: Node) -> set[bytes]:
    """Every name a function's parameters bind, destructured ones included."""
    params = func.child_by_field_name("parameters") or func.child_by_field_name("parameter")
    found: set[bytes] = set()
    pending = [params] if params is not None else []
    while pending:
        node = pending.pop()
        if node.type in ("identifier", "shorthand_property_identifier_pattern"):
            found.add(node.text)
        else:
            pending += [child for child in node.named_children if child.type != "type_annotation"]
    return found


def _call_functions(body: Node) -> list[Node]:
    """The `function` node of every call expression in `body`, and the name
    of every JSX element rendering a component (`_jsx_component`), not
    descending into nested function/class scopes - those are walked
    separately so calls are attributed to the correct enclosing scope rather
    than hoisted to the outer function (mirrors the Python extractor's
    `_call_functions`). An anonymous function (a callback, `items.map(i =>
    <Row />)`, an inline render prop; not one `_named_function` makes a
    Function of its own) is walked for its JSX elements only, which render
    from the enclosing function, unless their tag names a parameter of a
    callback holding them. `require(...)` is module-system syntax, captured
    by `_extract_imports`."""
    found: list[Node] = []

    def walk(node: Node, shadowed: frozenset[bytes] | None) -> None:
        if node.type in _NESTED_SCOPE_TYPES:
            if node.type not in _FUNCTION_VALUE_TYPES or _named_function(node):
                return
            shadowed = (shadowed or frozenset()) | _bound_names(node)
        if node.type == "call_expression" and shadowed is None:
            func = node.child_by_field_name("function")
            if func is not None and not (func.type == "identifier" and func.text == b"require"):
                found.append(func)
        elif node.type in _JSX_TAGS and (name := _jsx_component(node)) is not None:
            head = name if name.type == "identifier" else name.child_by_field_name("object")
            if shadowed is None or head is None or head.text not in shadowed:
                found.append(name)
        for child in node.children:
            walk(child, shadowed)

    walk(body, None)
    return found


def _extract_base_class_names(class_node: Node, source: bytes) -> list[str]:
    """Extract `extends` base-class name(s) from a class node's `class_heritage`.

    Handles both the plain-JS grammar's flat form (`class_heritage` ->
    `extends` + identifier/member_expression directly) and the TS grammar's
    wrapped form (`class_heritage` -> `extends_clause` [+ `implements_clause`]).
    `implements_clause` is intentionally ignored - see module docstring.
    """
    heritage = next((c for c in class_node.children if c.type == "class_heritage"), None)
    if heritage is None:
        return []

    bases: list[str] = []
    for child in heritage.named_children:
        if child.type == "extends_clause":
            if child.named_children:
                bases.append(_dotted_name(child.named_children[0], source))
        elif child.type in ("identifier", "member_expression", "nested_type_identifier"):
            bases.append(_dotted_name(child, source))
        # implements_clause: not an EXTENDS relationship, skipped.
    return bases


def _string_value(string_node: Node, source: bytes) -> str:
    """Extract a string literal node's text content (without quotes)."""
    frag = next((c for c in string_node.named_children if c.type == "string_fragment"), None)
    if frag is not None:
        return _text(frag, source)
    raw = _text(string_node, source)
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] in "'\"":
        return raw[1:-1]
    return raw


def _import_clause_names(node: Node, source: bytes) -> list[str]:
    """Extract bound local names from an `import_statement`'s `import_clause`
    (default import identifier, `* as ns` namespace import, and each name/
    alias in a `{ a, b as c }` named-imports list).
    """
    names: list[str] = []
    clause = next((c for c in node.named_children if c.type == "import_clause"), None)
    if clause is None:
        return names

    for child in clause.named_children:
        if child.type == "identifier":
            names.append(_text(child, source))
        elif child.type == "namespace_import":
            ns_name = next((c for c in child.named_children if c.type == "identifier"), None)
            if ns_name is not None:
                names.append(_text(ns_name, source))
        elif child.type == "named_imports":
            for spec in child.named_children:
                if spec.type == "import_specifier":
                    name_node = spec.child_by_field_name("name")
                    alias_node = spec.child_by_field_name("alias")
                    target_node = alias_node or name_node
                    if target_node is not None:
                        names.append(_text(target_node, source))
    return names


def _export_clause_names(node: Node, source: bytes) -> list[str]:
    """Extract re-exported names from `export { a, b as c } from '...'` /
    `export * from '...'` (the latter has no `export_clause`, so it returns
    a single '*' wildcard marker).
    """
    clause = next((c for c in node.named_children if c.type == "export_clause"), None)
    if clause is None:
        return ["*"]
    names = []
    for spec in clause.named_children:
        if spec.type == "export_specifier":
            name_node = spec.child_by_field_name("name")
            if name_node is not None:
                names.append(_text(name_node, source))
    return names or ["*"]


def _resolve_relative_base(current_dir: str, specifier: str) -> str:
    """Resolve a `./`/`../` specifier against the importing file's directory
    into a repo-relative base path (no extension yet).
    """
    parts = [p for p in current_dir.split("/") if p]
    for part in specifier.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _resolve_module_specifier(
    specifier: str, current_dir: str, config: ResolverConfig | None = None, file_path: str = ""
) -> list[str]:
    """Return candidate Module-node target names for an import specifier.

    A relative specifier with an explicit JS/TS extension (`./x.js`,
    `./y.mjs`) resolves to the sources that emit it: `.js` to `.ts`/`.tsx`/
    `.js`, `.jsx` to `.tsx`/`.jsx`, `.mjs` to `.mts`/`.mjs`, `.cjs` to
    `.cts`/`.cjs`; a TypeScript extension to itself; then `{path}/index.{ext}`
    in case it names a directory. Like every candidate list here this is not
    checked against the disk: `./x.js` links both `x.ts` and `x.js` when both
    exist. A `?query` or `#fragment` is dropped first.

    Other relative specifiers (`./foo`, `../bar/baz`) resolve to `{dir}/{path}.
    {ext}` candidates against the importing file's directory, across each of
    `.js/.jsx/.ts/.tsx`, plus `{dir}/{path}/index.{ext}` directory-import
    candidates - the same non-materializing-guess pattern the Python
    extractor uses for its own import targets (a candidate that doesn't
    match a real indexed Module node simply never produces an edge).

    A bare specifier the importing file's tsconfig maps (`paths`, else
    `baseUrl`), or that names a workspace package (`@acme/utils`, its entry
    point; `@acme/utils/x`, that subpath; see
    resolver_config.ResolverConfig.bare_import, given `config`), names the
    same candidates for each path it maps to. Any other bare specifier
    (`lodash`, `@scope/pkg`) is an external package: no candidate.
    """
    # A bundler query or fragment (`./worker.js?worker`, `./x#frag`) names no file.
    specifier = specifier.split("?", 1)[0].split("#", 1)[0]
    if specifier.startswith("."):
        return _base_candidates(_resolve_relative_base(current_dir, specifier))
    if config is not None and (found := config.bare_import(specifier, file_path)) is not None:
        return list(dict.fromkeys(candidate for base in found[0] for candidate in _base_candidates(base)))
    return []


def _specifier_pins(
    specifier: str, current_dir: str, config: ResolverConfig | None, file_path: str
) -> list[str] | None:
    """Where a name imported from `specifier` can be defined: its candidate
    files (`_resolve_module_specifier`) and the recursive prefix of each path
    it names, so a barrel's re-export reaches the defining file (as
    "package"; never the repository root), and of a workspace package's
    directory, whose entry point re-exports from anywhere in it. None for
    an external package."""
    specifier = specifier.split("?", 1)[0].split("#", 1)[0]
    packages: list[str] = []
    if specifier.startswith("."):
        bases = [_resolve_relative_base(current_dir, specifier)]
    elif config is not None and (found := config.bare_import(specifier, file_path)) is not None:
        bases, packages = found
    else:
        return None
    pins = []
    for base in bases:
        pins += _base_candidates(base)
        stem, dot, ext = base.rpartition(".")
        folder = stem if dot and "/" not in ext and f".{ext}" in _EXPLICIT_EXTENSIONS and stem else base
        if folder and "!" not in folder:  # graph/schema.py split_pin: no "!" in a path pin
            pins.append(folder + "/")
    pins += [folder + "/" for folder in packages if "!" not in folder]
    return list(dict.fromkeys(pins))


def _base_candidates(base: str) -> list[str]:
    """The files a resolved specifier path (repo-relative, no extension
    applied) can name: see `_resolve_module_specifier`."""
    stem, dot, ext = base.rpartition(".")
    mapped = _EXPLICIT_EXTENSIONS.get(f".{ext}") if dot and "/" not in ext else None
    if mapped and stem and not stem.endswith("/"):
        # Then the specifier as a directory, for a folder named like a file.
        return [f"{stem}.{each}" for each in mapped] + [f"{base}/index.{each}" for each in _EXTENSIONS]
    if base:
        candidates = [f"{base}.{ext}" for ext in _EXTENSIONS]
        candidates += [f"{base}/index.{ext}" for ext in _EXTENSIONS]
    else:
        # Specifier resolves to the repo root itself (e.g. `require('../..')`
        # from two directories down) - only the directory-import ('index.*')
        # form is meaningful; a bare '.js'/'.jsx'/etc with no basename isn't
        # a real candidate file path.
        candidates = [f"index.{ext}" for ext in _EXTENSIONS]
    return candidates


def _extract_imports(
    root: Node, source: bytes, current_dir: str, config: ResolverConfig | None = None, file_path: str = ""
) -> list[tuple[str, list[str]]]:
    """Extract import statements from the parse tree.

    Returns a list of (import_target, [bound_names]) tuples, where
    import_target is a candidate Module-node name IMPORTS should point at
    (see `_resolve_module_specifier`). Covers ES module `import ... from`,
    `export ... from` (re-exports), and CommonJS `require(...)` calls,
    whether standalone or as a `const x = require('y')` initializer.
    """
    imports: list[tuple[str, list[str]]] = []

    def walk(node: Node) -> None:
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                specifier = _string_value(source_node, source)
                names = _import_clause_names(node, source) or [specifier]
                for target in _resolve_module_specifier(specifier, current_dir, config, file_path):
                    imports.append((target, names))
        elif node.type == "export_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                specifier = _string_value(source_node, source)
                names = _export_clause_names(node, source)
                for target in _resolve_module_specifier(specifier, current_dir, config, file_path):
                    imports.append((target, names))
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier" and _text(func, source) == "require":
                args = node.child_by_field_name("arguments")
                if args is not None and args.named_children:
                    first_arg = args.named_children[0]
                    if first_arg.type == "string":
                        specifier = _string_value(first_arg, source)
                        for target in _resolve_module_specifier(specifier, current_dir, config, file_path):
                            imports.append((target, [specifier]))

        for child in node.children:
            walk(child)

    walk(root)
    return imports


@dataclass(frozen=True)
class _Binding:
    """A name an import binds: the callee name it stands for (the exported
    name of `{a as b}`, else the local name), where that can be defined
    (`_specifier_pins`; None for an external package), and whether it is a
    whole module (`* as ns`, `const ns = require(...)`). `top_level` is
    true for an ES named import (`{a}`: a module's top-level function when
    called) and an ES namespace (`* as ns`: its members are top-level ones);
    a default import or a `require` can bind anything. `default` marks an
    ES default import: a class it binds is its module's default-exported
    one, whatever the name (`DEFAULT_EXPORT`)."""

    name: str
    pins: tuple[str, ...] | None
    namespace: bool
    top_level: bool = False
    default: bool = False


@dataclass
class _ClassInfo:
    methods: set[str] = field(default_factory=set)
    bases: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)  # field -> the types it holds
    # What its members are owned by (common.member_owner): the enclosing
    # classes' names and its own, one per class of the name in the file.
    members_owners: set[str | None] = field(default_factory=set)

    def members_owner(self) -> str | None:
        """The owner of the class's members, None when several classes of
        the file share its name or it is declared in a function."""
        return next(iter(self.members_owners)) if len(self.members_owners) == 1 else None


@dataclass
class _Scope:
    """What a call site sees: the enclosing class, the parameters of its
    function (and enclosing ones) and the types of its typed names."""

    cls: str | None = None
    params: frozenset[str] = frozenset()
    types: dict[str, list[str]] = field(default_factory=dict)

    def nested(self, params: set[str], types: dict[str, list[str]]) -> _Scope:
        outer = {name: t for name, t in self.types.items() if name not in params}
        return _Scope(self.cls, self.params | params, {**outer, **types})


#: The owner a default import's class names (`import Thing from './thing';
#: new Thing().start()`): `export default class ThingImpl` is its heir, so
#: ThingImpl's members, and those it inherits, hold its heir key
#: (common.heir_keys). No class can be named `default`.
DEFAULT_EXPORT = "default"


def _default_export(root: Node, source: bytes, classes: dict[str, _ClassInfo]) -> str | None:
    """The class a file's `export default` names (`export default class
    X`, `export default X;`), or None."""
    for node in root.named_children:
        if node.type != "export_statement" or not any(child.type == "default" for child in node.children):
            continue
        target = node.child_by_field_name("declaration") or node.child_by_field_name("value")
        if target is not None and target.type in _CLASS_TYPES:
            target = target.child_by_field_name("name")
        if target is not None and target.type in ("identifier", "type_identifier") and _text(target, source) in classes:
            return _text(target, source)
    return None


def _class_like(name: str) -> bool:
    """Whether an imported name reads as a class (CapWords), not an
    instance or a module object (`api`, `DEFAULTS`)."""
    return name[:1].isupper() and not name.isupper()


#: Globals a classic script calls that are never a script's own function.
_JS_GLOBALS = frozenset({
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval", "requestAnimationFrame", "parseInt",
    "parseFloat", "isNaN", "isFinite", "alert", "confirm", "prompt", "encodeURIComponent", "decodeURIComponent",
    "encodeURI", "decodeURI", "structuredClone", "queueMicrotask", "String", "Number", "Boolean", "Array", "Object",
    "Symbol", "BigInt", "Date", "RegExp", "Error", "Promise", "require",
})

#: Receivers that are literals: `"".trim()`, `[].map()`, `({}).toString()`.
_LITERAL_RECEIVERS = frozenset({
    "string", "template_string", "array", "object", "number", "regex", "true", "false", "null", "undefined",
})


def _type_names(node: Node | None, source: bytes) -> list[str]:
    """The named types a type annotation can hold: `T`, `ns.T`, each named
    member of a union (`T | null`), a generic's own name; never an array
    type (`T[]`), whose methods are the array's."""
    if node is None:
        return []
    if node.type == "type_annotation":
        return [name for child in node.named_children for name in _type_names(child, source)]
    if node.type in ("type_identifier", "nested_type_identifier"):
        return [_dotted_name(node, source)]
    if node.type in ("union_type", "parenthesized_type"):
        return [name for child in node.named_children for name in _type_names(child, source)]
    if node.type == "generic_type":
        name = node.child_by_field_name("name")
        return [_dotted_name(name, source)] if name is not None else []
    return []


def _constructed(node: Node | None, source: bytes) -> str | None:
    """`T` (or `ns.T`) of a `new T(...)` expression."""
    if node is not None and node.type == "new_expression":
        ctor = node.child_by_field_name("constructor")
        if ctor is not None and ctor.type in ("identifier", "member_expression"):
            return _dotted_name(ctor, source)
    return None


def _parameters(func: Node, source: bytes) -> tuple[set[str], dict[str, list[str]], dict[str, list[str]]]:
    """A function's parameter names, the types its annotated ones hold, and
    the fields its constructor parameter properties declare
    (`constructor(private repo: Repo)` types `this.repo`)."""
    names: set[str] = set()
    types: dict[str, list[str]] = {}
    fields: dict[str, list[str]] = {}
    params = func.child_by_field_name("parameters")
    if params is None and func.type == "arrow_function":
        single = func.child_by_field_name("parameter")
        if single is not None and single.type == "identifier":
            names.add(_text(single, source))
    for param in params.named_children if params is not None else []:
        pattern = param.child_by_field_name("pattern") if param.type in (
            "required_parameter", "optional_parameter"
        ) else param
        if pattern is None or pattern.type != "identifier":
            continue
        name = _text(pattern, source)
        names.add(name)
        annotated = _type_names(param.child_by_field_name("type"), source)
        if annotated:
            types[name] = annotated
            if any(c.type in ("accessibility_modifier", "readonly") for c in param.children):
                fields[name] = annotated
    return names, types, fields


def _local_types(body: Node, source: bytes) -> dict[str, list[str]]:
    """Names a function body declares with a type annotation or a `new T()`
    value, outside nested scopes."""
    types: dict[str, list[str]] = {}

    def walk(node: Node) -> None:
        if node.type in _NESTED_SCOPE_TYPES:
            return
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                annotated = _type_names(node.child_by_field_name("type"), source)
                constructed = _constructed(node.child_by_field_name("value"), source)
                if annotated or constructed:
                    types[_text(name, source)] = annotated or [constructed]
        for child in node.children:
            walk(child)

    walk(body)
    return types


#: Every node a function's body hangs off.
_FUNCTION_NODE_TYPES = frozenset({
    "function_declaration", "generator_function_declaration", "function_expression", "function",
    "generator_function", "arrow_function", "method_definition",
})


def _class_table(root: Node, source: bytes) -> dict[str, _ClassInfo]:
    """Every class in the file: its methods, its `extends` bases and the
    types of its fields (annotated, `= new T()`, or constructor parameter
    properties)."""
    classes: dict[str, _ClassInfo] = {}

    def walk(node: Node, owner: str | None) -> None:
        if node.type in _FUNCTION_NODE_TYPES:
            # A class inside a function (owned by its functions too) has an
            # owner this walk doesn't follow: its members are any of the name.
            owner = None
        if node.type in _CLASS_TYPES and (name := node.child_by_field_name("name")) is not None:
            info = classes.setdefault(_text(name, source), _ClassInfo())
            owner = member_owner("Class", _text(name, source), owner) if owner is not None else None
            info.members_owners.add(owner)
            info.bases += _extract_base_class_names(node, source)
            body = node.child_by_field_name("body")
            for member in body.named_children if body is not None else []:
                member_name = member.child_by_field_name("name") or member.child_by_field_name("property")
                if member_name is None:
                    continue
                if member.type == "method_definition":
                    info.methods.add(_text(member_name, source))
                    if _text(member_name, source) == "constructor":
                        info.fields.update(_parameters(member, source)[2])
                elif member.type in ("field_definition", "public_field_definition"):
                    value = member.child_by_field_name("value")
                    if value is not None and value.type in _FUNCTION_VALUE_TYPES:
                        info.methods.add(_text(member_name, source))
                        continue
                    held = _type_names(member.child_by_field_name("type"), source)
                    constructed = _constructed(value, source)
                    if held or constructed:
                        info.fields[_text(member_name, source)] = held or [constructed]
        for child in node.children:
            walk(child, owner)

    walk(root, "")
    return classes


def _defined_functions(root: Node, source: bytes) -> set[str]:
    """Names a bare call can reach in this file: function declarations and
    functions bound to a name (`const f = () => {}`), at any depth; not
    methods."""
    names: set[str] = set()

    def walk(node: Node) -> None:
        if node.type in ("function_declaration", "generator_function_declaration"):
            name = node.child_by_field_name("name")
            if name is not None:
                names.add(_text(name, source))
        elif node.type == "variable_declarator":
            name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
            if name is not None and name.type == "identifier" and _function_value(value) is not None:
                names.add(_text(name, source))
        for child in node.children:
            walk(child)

    walk(root)
    return names


def _bindings(
    root: Node, source: bytes, current_dir: str, config: ResolverConfig | None, file_path: str
) -> tuple[dict[str, _Binding], bool]:
    """The names the file's imports bind (`import` statements, and
    `const x = require(...)` / `const {a, b: c} = require(...)`), and
    whether it is a module at all: a classic script has no `import`,
    `export` or `require`."""
    bindings: dict[str, _Binding] = {}
    module = False

    def pins(node: Node | None) -> tuple[str, ...] | None:
        if node is None or node.type != "string":
            return None
        found = _specifier_pins(_string_value(node, source), current_dir, config, file_path)
        return tuple(found) if found is not None else None

    def walk(node: Node) -> None:
        nonlocal module
        if node.type in ("import_statement", "export_statement"):
            module = True
        if node.type == "import_statement":
            where = pins(node.child_by_field_name("source"))
            clause = next((c for c in node.named_children if c.type == "import_clause"), None)
            for child in clause.named_children if clause is not None else []:
                if child.type == "identifier":
                    name = _text(child, source)
                    bindings[name] = _Binding(name, where, False, default=True)
                elif child.type == "namespace_import":
                    ns = next((c for c in child.named_children if c.type == "identifier"), None)
                    if ns is not None:
                        bindings[_text(ns, source)] = _Binding(_text(ns, source), where, True, True)
                elif child.type == "named_imports":
                    for spec in child.named_children:
                        name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                        if spec.type == "import_specifier" and name is not None:
                            bindings[_text(alias or name, source)] = _Binding(_text(name, source), where, False, True)
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier" and func.text == b"require":
                module = True
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            func = value.child_by_field_name("function") if value is not None and value.type == "call_expression" \
                else None
            if func is not None and func.type == "identifier" and func.text == b"require":
                args = value.child_by_field_name("arguments")
                where = pins(args.named_children[0]) if args is not None and args.named_children else None
                name = node.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    bindings[_text(name, source)] = _Binding(_text(name, source), where, True)
                elif name is not None and name.type == "object_pattern":
                    for prop in name.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            bindings[_text(prop, source)] = _Binding(_text(prop, source), where, False)
                        elif prop.type == "pair_pattern":
                            key, local = prop.child_by_field_name("key"), prop.child_by_field_name("value")
                            if key is not None and local is not None and local.type == "identifier":
                                bindings[_text(local, source)] = _Binding(_text(key, source), where, False)
        for child in node.children:
            walk(child)

    walk(root)
    return bindings, module


class _CallResolver:
    """Resolves a JS/TS call to the files its callee can be in (see the
    module docstring's tiers): each target is (callee name, pin, member
    call?), the pin a file, a package directory ending in "/", or None for a
    bare-name match."""

    def __init__(
        self, file_path: str, bindings: dict[str, _Binding], classes: dict[str, _ClassInfo],
        defined: set[str], script: bool, source: bytes,
    ):
        self.file_path = file_path
        self.bindings = bindings
        self.classes = classes
        self.defined = defined
        self.script = script
        self.source = source

    def _binding_pins(self, name: str) -> list[str] | None:
        binding = self.bindings.get(name)
        if binding is None:
            return None
        return list(binding.pins) if binding.pins is not None else []

    def _class_pins(self, type_name: str, method: str) -> list[str] | None:
        """Where `method` of the class `type_name` (as written) can be: an
        in-file class is walked like `this` (`_method_pins`); an imported one
        is its import's files; `ns.T` is the namespace's."""
        if type_name in self.classes:
            return self._method_pins(type_name, method, own=True)
        head, dot, rest = type_name.partition(".")
        binding = self.bindings.get(head)
        if binding is not None and (dot == "" or binding.namespace):
            # An imported class's method: the class's member there, its own
            # or one it inherits from a class of its file (common.heir_keys);
            # through a default import, the default-exported class's
            # (DEFAULT_EXPORT); any of the name through a require.
            owner = (rest if "." not in rest else None) if dot else binding.name
            if not binding.top_level:
                owner = DEFAULT_EXPORT if binding.default and not dot else None
            return [class_member_pin(pin, owner) for pin in binding.pins or ()]
        return None

    def _method_pins(self, cls: str, method: str, own: bool) -> list[str] | None:
        """Where `this.method()` (own) or `super.method()` in `cls` can go:
        this file when `cls` or an in-file base defines it, as that class's
        member (common.owned_pin), the files of an imported base (not
        followed further); None when nothing does."""
        if own and method in self.classes[cls].methods:
            return [owned_pin(self.file_path, self.classes[cls].members_owner())]
        seen = {cls}
        pending = list(self.classes[cls].bases)
        while pending:
            base = pending.pop(0)
            if base in self.classes:
                if base in seen:
                    continue
                seen.add(base)
                if method in self.classes[base].methods:
                    return [owned_pin(self.file_path, self.classes[base].members_owner())]
                pending += self.classes[base].bases
            elif (pins := self._class_pins(base, method)) is not None:
                return pins
        return None

    def _receiver_pins(self, obj: Node, method: str, scope: _Scope) -> list[str] | None:
        """Where `method` called on `obj` can be, or None when nothing types it."""
        source = self.source
        if obj.type == "this" and scope.cls in self.classes:
            return self._method_pins(scope.cls, method, own=True)
        if obj.type == "super" and scope.cls in self.classes:
            return self._method_pins(scope.cls, method, own=False)
        if obj.type == "member_expression":
            inner, prop = obj.child_by_field_name("object"), obj.child_by_field_name("property")
            if inner is not None and inner.type == "this" and prop is not None and scope.cls in self.classes:
                held = self.classes[scope.cls].fields.get(_text(prop, source), [])
                return self._typed_pins(held, method)
            return None
        if (constructed := _constructed(obj, source)) is not None:
            return self._class_pins(constructed, method)
        if obj.type != "identifier":
            return None
        receiver = _text(obj, source)
        if receiver in scope.types:
            return self._typed_pins(scope.types[receiver], method)
        if receiver in scope.params:
            return None  # an untyped parameter shadows any import of its name
        binding = self.bindings.get(receiver)
        if binding is not None:
            # An ES namespace's member is a module's top-level one, a named or
            # default import's of a class (a CapWords name) the class's
            # (`_class_pins`); anything else's (a require, an instance) any of
            # the name.
            if (binding.top_level and not binding.namespace or binding.default) and _class_like(receiver):
                return self._class_pins(receiver, method)
            owner = "" if binding.namespace and binding.top_level else None
            return [owned_pin(pin, owner) for pin in binding.pins or ()]
        if receiver in self.classes:
            return self._class_pins(receiver, method)
        if receiver in STOP_TYPES["js"]:
            return []
        return None

    def render_pins(self, tag: Node, scope: _Scope) -> list[str]:
        """Where the `render` of the class component a JSX tag names can be:
        a class of the file (or its in-file base), as `this.render()`
        would go; an imported class's, as its member (`_class_pins`: a
        named, default or namespace import's owner, never an owner-less
        pin, which would link every `render` there). Empty for a function
        component of the file, a parameter, or a name nothing imports."""
        name = _text(tag, self.source)
        head = name.split(".", 1)[0]
        if head in scope.params or (head == name and name in self.defined and name not in self.classes):
            return []
        if name in self.classes:
            return self._method_pins(name, "render", own=True) or []
        binding = self.bindings.get(head)
        if binding is None or (head == name and binding.namespace):
            return []
        return [pin for pin in self._class_pins(name, "render") or () if split_owned_pin(pin)[1] is not None]

    def _typed_pins(self, types: list[str], method: str) -> list[str] | None:
        found = [pins for name in types if (pins := self._class_pins(name, method)) is not None]
        return sorted({pin for pins in found for pin in pins}) if found else None

    def resolve(self, func: Node, scope: _Scope) -> list[tuple[str, str | None, bool]]:
        source = self.source
        if func.type == "identifier":
            name = _text(func, source)
            if name in scope.params:
                return []  # calling a parameter: it shadows any function of its name
            if name in self.defined:
                return [(name, self.file_path, False)]
            binding = self.bindings.get(name)
            if binding is not None:  # an ES named import's top-level function; else any of the name
                owner = "" if binding.top_level else None
                return [(binding.name, owned_pin(pin, owner), False) for pin in binding.pins or ()]
            if self.script and name not in _JS_GLOBALS:
                return [(name, None, False)]  # a classic script's globals are shared
            return []
        if func.type != "member_expression":
            return []
        obj, prop = func.child_by_field_name("object"), func.child_by_field_name("property")
        if obj is None or prop is None or prop.type not in ("property_identifier", "private_property_identifier"):
            return []
        method = _text(prop, source)
        pins = self._receiver_pins(obj, method, scope)
        if pins is not None:
            return [(method, pin, True) for pin in pins]
        if obj.type in _LITERAL_RECEIVERS or method in STOP_METHODS["js"]:
            return []
        return [(method, None, True)]


#: Express/Koa-router/Fastify route methods; `all` maps every method.
_ROUTE_METHODS = frozenset({"get", "post", "put", "delete", "patch", "options", "head", "all"})
#: Calls that make an app or a router (`express()`, `express.Router()`,
#: `Router()`, `new Router()`, `Fastify()`), by the callee's last name.
_ROUTER_FACTORIES = frozenset({"express", "Router", "fastify", "Fastify"})
#: Receivers taken for an app or router by name alone (and any `...Router`).
_ROUTER_NAMES = frozenset({"app", "router", "fastify"})
_HANDLER_TYPES = frozenset({"identifier", "member_expression", "call_expression", *_FUNCTION_VALUE_TYPES})


def _literal(node: Node | None, source: bytes) -> str | None:
    """A string literal's value, or a template literal's with nothing
    substituted; None for anything else (a dynamic value)."""
    if node is None:
        return None
    if node.type == "string":
        return _string_value(node, source)
    if node.type == "template_string" and not any(c.type == "template_substitution" for c in node.named_children):
        return _text(node, source)[1:-1]
    return None


def _express_routes(
    root: Node, source: bytes, resolver: _CallResolver, result: ExtractionResult, file_path: str, repo_id: str
) -> None:
    """Express-style routes (`app.get('/x', h)`, `router.post(...)`,
    `app.route('/x').get(h)`; Koa-router's and Fastify's are the same
    calls) as Endpoints (apis/routes.py). The receiver is an app or router
    the file makes (`_ROUTER_FACTORIES`, held by a name or by a class's
    `this.router`) or one named like one (`_ROUTER_NAMES`, `...Router`);
    the path is a literal starting with "/", and the last argument is the
    handler. An app's routes are at its root; a router's are under the
    literal prefix the file mounts it at (`app.use('/api', router)`), and
    a router the file doesn't mount on its app is mounted where the file
    doesn't show (`unmounted_endpoint`)."""
    made: dict[str, bool] = {}  # receiver -> whether the file made an app (else a router)
    mounts: dict[str, tuple[str, str]] = {}  # router -> (the app or router it is mounted on, the prefix)
    routes: list[tuple[str, str, str, Node]] = []  # (receiver, method, path, handler)

    def key(node: Node | None) -> str | None:
        """A receiver as the file names it: `app`, or `this.router`."""
        if node is not None and node.type == "identifier":
            return _text(node, source)
        if node is not None and node.type == "member_expression":
            obj, prop = node.child_by_field_name("object"), node.child_by_field_name("property")
            if obj is not None and obj.type == "this" and prop is not None:
                return f"this.{_text(prop, source)}"
        return None

    def factory(value: Node | None) -> bool | None:
        """Whether `value` makes an app (True) or a router (False); None when neither."""
        if value is None or value.type not in ("call_expression", "new_expression"):
            return None
        callee = value.child_by_field_name("function") or value.child_by_field_name("constructor")
        name = _dotted_name(callee, source).rsplit(".", 1)[-1] if callee is not None else ""
        if name not in _ROUTER_FACTORIES and not name.endswith("Router"):
            return None
        return not name.endswith("Router")

    def callee(call: Node) -> tuple[Node, str] | None:
        """(receiver, method name) of a `receiver.method(...)` call."""
        func = call.child_by_field_name("function")
        if func is None or func.type != "member_expression":
            return None
        obj, prop = func.child_by_field_name("object"), func.child_by_field_name("property")
        return (obj, _text(prop, source)) if obj is not None and prop is not None else None

    def visit_call(call: Node) -> None:
        found = callee(call)
        args = call.child_by_field_name("arguments")
        if found is None or args is None:
            return
        obj, method = found
        values = [arg for arg in args.named_children if arg.type != "comment"]
        if method == "use" and key(obj) is not None and len(values) == 2 and key(values[1]) is not None:
            mounted = _literal(values[0], source)
            if mounted is not None and mounted.startswith("/"):
                mounts[key(values[1])] = (key(obj), mounted)
            return
        if method not in _ROUTE_METHODS:
            return
        path, handlers = None, values
        if obj.type == "call_expression":
            # `app.route('/x').get(h).post(h2)`: down the chain to its `route`.
            while obj.type == "call_expression" and (inner := callee(obj)) is not None and inner[1] in _ROUTE_METHODS:
                obj = inner[0]
            inner = callee(obj) if obj.type == "call_expression" else None
            route_args = obj.child_by_field_name("arguments") if inner is not None and inner[1] == "route" else None
            if route_args is None or not route_args.named_children:
                return
            path, obj = _literal(route_args.named_children[0], source), inner[0]
        elif values and (path := _literal(values[0], source)) is not None:
            handlers = values[1:]
            if not path.startswith("/") and len(values) > 2:
                # Koa-router's named route: `router.get('name', '/x', h)`.
                path, handlers = _literal(values[1], source), values[2:]
        if path is None or not path.startswith("/") or not handlers or handlers[-1].type not in _HANDLER_TYPES:
            return
        if key(obj) is not None:
            routes.append((key(obj), "*" if method == "all" else method.upper(), path, handlers[-1]))

    def walk(node: Node) -> None:
        if node.type == "variable_declarator":
            name = node.child_by_field_name("name")
            makes = factory(node.child_by_field_name("value"))
            if name is not None and name.type == "identifier" and makes is not None:
                made[_text(name, source)] = makes
        elif node.type == "assignment_expression":
            # `this.router = express.Router()` in a constructor.
            target, makes = key(node.child_by_field_name("left")), factory(node.child_by_field_name("right"))
            if target is not None and target.startswith("this.") and makes is not None:
                made[target] = makes
        elif node.type in ("field_definition", "public_field_definition"):
            # `router = Router();` in a class body.
            name = node.child_by_field_name("name") or node.child_by_field_name("property")
            makes = factory(node.child_by_field_name("value"))
            if name is not None and makes is not None:
                made[f"this.{_text(name, source)}"] = makes
        elif node.type == "call_expression":
            visit_call(node)
        for child in node.children:
            walk(child)

    walk(root)

    def is_app(receiver: str) -> bool:
        return made.get(receiver, receiver in ("app", "fastify"))

    def prefix(receiver: str, seen: frozenset = frozenset()) -> str | None:
        """The literal prefix of a receiver's routes; None when the file
        doesn't mount it on an app."""
        if is_app(receiver):
            return ""
        if receiver not in mounts or receiver in seen:
            return None
        parent, mounted = mounts[receiver]
        above = prefix(parent, seen | {receiver})
        return None if above is None else join_route(above, mounted)

    # The file's functions, by name, with their owners.
    functions: dict[str, set[str]] = {}
    for node in result.nodes:
        if node.label == "Function" and node.properties.get("file") == file_path:
            functions.setdefault(node.name, set()).add(node.properties.get("owner") or "")
    for receiver, method, path, handler in routes:
        if receiver not in made and receiver not in _ROUTER_NAMES and not (
            receiver.endswith("Router") and not receiver.startswith("this.")
        ):
            continue
        above = prefix(receiver)
        full = join_route(above or "", path)
        mounted = above is not None
        if handler.type == "identifier" and _text(handler, source) in functions:
            # A function of the file: the top-level one, else any of the name.
            name = _text(handler, source)
            owner = "" if "" in functions[name] else None
            add_route(result, repo_id, file_path, "Express", method, full, name, owner, mounted=mounted)
        elif handler.type in ("identifier", "member_expression"):
            # An imported function, or a member of an imported or typed
            # value: where a call to it would go.
            targets = resolver.resolve(handler, _Scope())
            if handler.type == "member_expression":
                name = _text(handler.child_by_field_name("property"), source)
            else:
                name = targets[0][0] if targets else _text(handler, source)
            pins = {pin for _name, pin, _member in targets if pin is not None}
            add_route(result, repo_id, file_path, "Express", method, full, name, pins=pins, mounted=mounted)
        else:
            add_route(result, repo_id, file_path, "Express", method, full, mounted=mounted)


def extract_js_file(
    source_code: str, file_path: str, repo_id: str, config: ResolverConfig | None = None
) -> ExtractionResult:
    """Parse a JS/TS/JSX/TSX file and extract nodes and relationships.

    Args:
        source_code: The file's source as a string.
        file_path: The Module node's identity - the file's path relative to
            the repo root, forward-slashed (e.g. 'src/services/api.ts').
            Also used to pick the Tree-sitter grammar, by extension.
        repo_id: Repository ID for scoping nodes.
        config: The repository's resolver configuration (its tsconfig path
            aliases), read once per batch; None resolves no alias.

    Returns:
        ExtractionResult containing lists of nodes and relationships. On a
        syntax error, Tree-sitter still returns a best-effort tree (ERROR
        nodes rather than raising), so partial results are extracted and the
        error is logged rather than the whole file being skipped.
    """
    result = ExtractionResult()
    source_bytes = source_code.encode("utf-8")

    language = _language_for_extension(Path(file_path).suffix)
    parser = _make_parser(language)
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

    current_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
    for target, names in _extract_imports(root, source_bytes, current_dir, config, file_path):
        for _name in names:
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

    bindings, module = _bindings(root, source_bytes, current_dir, config, file_path)
    class_table = _class_table(root, source_bytes)
    resolver = _CallResolver(
        file_path, bindings, class_table, _defined_functions(root, source_bytes), not module, source_bytes,
    )
    # (caller label, caller name, caller owner)
    #   -> callee name -> [pins, bare?, caller classes, every bare site a member call?]
    calls: dict[tuple[str, str, str | None], dict[str, list]] = {}
    # The owner (common.member_owner) of the class or function being visited,
    # innermost last; "" at the Module.
    owners = [""]

    def record_calls(
        caller_label: str, caller_name: str, body: Node, scope: _Scope, caller_class: str | None,
        caller_owner: str | None,
    ) -> None:
        by_name = calls.setdefault((caller_label, caller_name, caller_owner), {})
        for func in _call_functions(body):
            jsx = func.parent is not None and func.parent.type in _JSX_TAGS
            # A class component renders through its `render`, which its class owns.
            rendered = [("render", pin, False) for pin in resolver.render_pins(func, scope)] if jsx else []
            for name, pin, member in resolver.resolve(func, scope) + rendered:
                if pin is None and jsx:
                    continue  # a component nothing resolves links nothing, never by name alone
                entry = by_name.setdefault(name, [set(), False, set(), True])
                if pin is None:
                    entry[1] = True
                    entry[3] = entry[3] and member
                else:
                    entry[0].add(pin)
                if caller_class:
                    entry[2].add(caller_class)

    def _try_visit_definition(node: Node, parent_name: str | None, parent_label: str, scope: _Scope) -> bool:
        """If `node` (a statement) is a class/function/named-function-const
        definition, visit it as such and return True. Otherwise return False
        without touching calls - the caller decides what a non-definition
        statement means at its own level (see `_visit_statement` vs.
        `_visit_nested_defs`).
        """
        target = node
        if node.type == "export_statement":
            decl = node.child_by_field_name("declaration")
            if decl is None:
                return False  # bare `export {...}` / `export * from '...'`
            target = decl

        if target.type in _CLASS_TYPES:
            _visit_class(target, parent_name, parent_label, node, scope)
            return True
        if target.type in ("function_declaration", "generator_function_declaration"):
            name_node = target.child_by_field_name("name")
            if name_node is not None:
                _visit_function(target, parent_name, parent_label, node, name_node, scope)
                return True
            return False
        if target.type in ("lexical_declaration", "variable_declaration"):
            handled = False
            first = True
            for decl_node in target.named_children:
                if decl_node.type != "variable_declarator":
                    continue
                name_node = decl_node.child_by_field_name("name")
                value_node = decl_node.child_by_field_name("value")
                is_named_fn = (
                    name_node is not None
                    and name_node.type == "identifier"
                    and _function_value(value_node) is not None
                )
                if is_named_fn:
                    _visit_variable_declarator(decl_node, parent_name, parent_label, node if first else None, scope)
                    handled = True
                first = False
            return handled
        return False

    def _visit_statement(node: Node, parent_name: str | None, parent_label: str, scope: _Scope) -> None:
        """Visit a Module-top-level statement: definitions become Class/
        Function nodes; anything else is scanned for call expressions
        attributed to the enclosing Module (mirrors the Python extractor's
        module-level-statement handling, e.g. `setup();` at top level).
        """
        if _try_visit_definition(node, parent_name, parent_label, scope):
            return
        if node.type == "export_statement":
            return  # declaration-less export - nothing left to scan for calls
        caller_class = parent_name if parent_label == "Class" else None
        record_calls(
            parent_label, parent_name if parent_name else file_path, node, scope, caller_class,
            owners[-1] if parent_label != "Module" else None,
        )

    def _visit_nested_defs(container: Node, parent_name: str, parent_label: str, scope: _Scope) -> None:
        """Visit a function body's direct statement children for nested
        named function/class/const-arrow definitions only - NOT a general
        call-scanning pass, since the caller (`_visit_function`) already ran
        `record_calls` over the whole body once, recursively
        (excluding nested scopes). Re-scanning non-definition statements
        here would double-emit CALLS edges for that body's top-level calls.
        Only direct children are checked (not statements nested inside an
        if/for/while block) - the same one-level limitation the Python
        extractor has for nested `def`s.
        """
        for node in container.named_children:
            _try_visit_definition(node, parent_name, parent_label, scope)

    def _visit_variable_declarator(
        decl_node: Node, parent_name: str | None, parent_label: str, doc_anchor: Node | None, scope: _Scope
    ) -> None:
        name_node = decl_node.child_by_field_name("name")
        value_node = decl_node.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier" or value_node is None:
            return
        if (function := _function_value(value_node)) is not None:
            # doc_anchor is None for a non-first declarator in a multi-name
            # `const a = ..., b = ...` statement - no JSDoc attributed to it.
            # A component wrapper's function (`memo(() => ...)`) is the
            # declarator's; a styled component is one with no body.
            _visit_function(function, parent_name, parent_label, doc_anchor or value_node, name_node, scope)

    def _visit_class(node: Node, parent_name: str | None, parent_label: str, doc_anchor: Node, scope: _Scope) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)
        base_classes = _extract_base_class_names(node, source_bytes)

        parent_owner = owners[-1] if parent_label != "Module" else None
        owner = member_owner(parent_label, parent_name, parent_owner)
        class_properties: dict = {
            "type": "class",
            "file": file_path,
            "owner": owner,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        jsdoc = _extract_jsdoc(doc_anchor, source_bytes)
        if jsdoc:
            class_properties["description"] = _docstring_summary(jsdoc)
            class_properties["docstring_full"] = jsdoc

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
                from_owner=parent_owner,
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

        body_node = node.child_by_field_name("body")
        if body_node is None:
            return
        class_scope = _Scope(class_name, scope.params, scope.types)
        owners.append(owner)
        _visit_members(body_node, class_name, class_scope)
        owners.pop()

    def _visit_members(body_node: Node, class_name: str, class_scope: _Scope) -> None:
        for member in body_node.named_children:
            if member.type == "method_definition":
                m_name_node = member.child_by_field_name("name")
                if m_name_node is not None and m_name_node.type == "property_identifier":
                    _visit_function(member, class_name, "Class", member, m_name_node, class_scope)
            elif member.type in ("field_definition", "public_field_definition"):
                # tree-sitter-typescript names the field `name`, tree-sitter-javascript `property`.
                f_name_node = member.child_by_field_name("name") or member.child_by_field_name("property")
                value_node = member.child_by_field_name("value")
                if (
                    f_name_node is not None
                    and f_name_node.type == "property_identifier"
                    and value_node is not None
                    and value_node.type in _FUNCTION_VALUE_TYPES
                ):
                    _visit_function(value_node, class_name, "Class", member, f_name_node, class_scope)

    def _visit_function(
        node: Node, parent_name: str | None, parent_label: str, doc_anchor: Node, name_node: Node, scope: _Scope
    ) -> None:
        func_name = _text(name_node, source_bytes)

        parent_owner = owners[-1] if parent_label != "Module" else None
        owner = member_owner(parent_label, parent_name, parent_owner)
        func_properties: dict = {
            "type": "function",
            "file": file_path,
            "owner": owner,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
        }
        jsdoc = _extract_jsdoc(doc_anchor, source_bytes)
        if jsdoc:
            func_properties["description"] = _docstring_summary(jsdoc)
            func_properties["docstring_full"] = jsdoc

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
                from_owner=parent_owner,
                to_owner=owner,
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is None:
            return

        caller_class = parent_name if parent_label == "Class" else None
        params, param_types, _fields = _parameters(node, source_bytes)
        inner = scope.nested(params, {**param_types, **_local_types(body_node, source_bytes)})
        record_calls("Function", func_name, body_node, inner, caller_class, owner)

        # Nested function/class declarations - only meaningful when the body
        # is an actual statement block; a concise arrow body (`() => expr`)
        # can't syntactically contain a nested declaration statement. Uses
        # _visit_nested_defs (definitions only), NOT _visit_statement, since
        # the call scan above already covers every call in this body.
        if body_node.type == "statement_block":
            owners.append(owner)
            _visit_nested_defs(body_node, func_name, "Function", inner)
            owners.pop()

    module_scope = _Scope(types=_local_types(root, source_bytes))
    for node in root.named_children:
        _visit_statement(node, None, "Module", module_scope)
    for (caller_label, caller_name, caller_owner), by_name in sorted(
        calls.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
    ):
        for name, (pins, bare, classes, no_self) in sorted(by_name.items()):
            result.relationships.extend(call_rows(
                caller_label, caller_name, name, pins, bare, min(classes, default=None), file_path, repo_id,
                no_self=no_self, caller_owner=caller_owner,
            ))
    _express_routes(root, source_bytes, resolver, result, file_path, repo_id)
    default = _default_export(root, source_bytes, class_table)
    add_heir_keys(result, [
        (name, owner, info.bases, info.methods) for name, info in class_table.items() for owner in info.members_owners
    ] + ([(DEFAULT_EXPORT, DEFAULT_EXPORT, [default], ())] if default else []))

    return own_edges(result, file_path)


def index_file(
    engine,
    repo_id: str,
    file_path: str | Path,
    repo_root: str | Path | None = None,
) -> None:
    """Extract a JS/TS file and upsert results into the graph.

    Thin wrapper mirroring `python/extractor.py`'s `index_file`: calls
    `extract_js_file()` then upserts each node and relationship via the
    GraphEngine.

    Args:
        engine: A GraphEngine instance.
        repo_id: Repository ID for scoping.
        file_path: Path to the JS/TS file to index.
        repo_root: The repository's root directory. When given, the Module
            node is keyed by file_path's path relative to repo_root (forward
            slashes) - required for relative-import resolution and to avoid
            same-named files in different directories colliding into one
            Module node. When omitted, falls back to the bare filename.

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

    result = extract_js_file(source_code, module_name, repo_id)

    engine.upsert_nodes([node.to_dict() for node in result.nodes])
    engine.upsert_relationships([rel.to_dict() for rel in result.relationships])
