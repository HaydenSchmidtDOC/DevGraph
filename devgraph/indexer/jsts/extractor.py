"""Tree-sitter-based JavaScript/TypeScript source-code extractor.

Mirrors `devgraph/indexer/python/extractor.py`'s shape (Implementation Plan
#8, row 1: JS/TS), reusing the shared `GraphNode`/`GraphRelationship`/
`ExtractionResult` dataclasses from `devgraph.indexer.common` rather than
redefining them.

Parses a .js/.jsx/.ts/.tsx file and extracts:
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
  - IMPORTS relationships from ES module `import`/`export ... from` syntax
    and CommonJS `require()` calls.
  - JSDoc (`/** ... */`) comments immediately preceding a class/function as
    that node's `description`/`docstring_full` properties (module-level
    JSDoc is not extracted - out of scope, the brief only asks for
    class/function docstrings).

Grammar routing is purely by file extension: '.ts' uses the TypeScript
grammar, '.tsx' uses the TSX grammar (JSX + TS types), and everything else
('.js', '.jsx', and any unrecognized extension) uses the plain JavaScript
grammar. One function (`extract_js_file`) handles all four extensions rather
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
    candidates) - the same non-materializing-guess pattern the Python
    extractor uses (a guessed target that doesn't match a real indexed file
    simply never produces an edge, since upsert_relationship only
    MATCH-links real existing nodes). Bare specifiers (`import x from
    'lodash'`) get a single best-effort `node_modules/{name}` guess; no
    `package.json`/`tsconfig.json` `paths` mapping is consulted (out of
    scope per the plan - "don't over-engineer"). No pnpm/yarn workspace
    resolution.
"""

from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from devgraph.indexer.common import ExtractionResult, GraphNode, GraphRelationship

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


def _make_parser(language: Language) -> Parser:
    return Parser(language)


def _language_for_extension(suffix: str) -> Language:
    suffix = suffix.lower()
    if suffix == ".ts":
        return _TS_LANGUAGE
    if suffix == ".tsx":
        return _TSX_LANGUAGE
    return _JS_LANGUAGE  # '.js', '.jsx', and any unrecognized extension


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


def _callee_simple_name(func_node: Node, source: bytes) -> str | None:
    """Resolve a call expression's `function` field to a simple callee name.

    Same philosophy as the Python extractor's `_callee_simple_name`:
    `foo()` -> 'foo', `obj.foo()`/`this.foo()` -> 'foo' (the member name,
    not type-resolved - a structural choice that intentionally over-links
    same-named methods/functions rather than under-linking).
    """
    if func_node.type == "identifier":
        return _text(func_node, source)
    if func_node.type == "member_expression":
        prop = func_node.child_by_field_name("property")
        if prop is not None:
            return _text(prop, source)
    if func_node.type == "call_expression":
        # Chained/immediately-invoked call, e.g. `getHandler()()`.
        inner = func_node.child_by_field_name("function")
        if inner is not None:
            return _callee_simple_name(inner, source)
    return None


def _extract_call_targets(body: Node, source: bytes) -> list[str]:
    """Walk a function/method body for call expressions, returning callee
    simple names. Does not descend into nested function/class scopes - those
    are walked separately so calls are attributed to the correct enclosing
    scope rather than hoisted to the outer function (mirrors the Python
    extractor's `_extract_call_targets`).
    """
    targets: list[str] = []

    def walk(node: Node) -> None:
        if node.type in _NESTED_SCOPE_TYPES:
            return
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None:
                name = _callee_simple_name(func, source)
                # 'require(...)' is module-system syntax already captured by
                # _extract_imports as an IMPORTS edge - not an app-level call
                # worth a CALLS edge (and 'require' is never itself indexed
                # as a Function node, so the edge would just be dead noise).
                if name and name != "require":
                    targets.append(name)
        for child in node.children:
            walk(child)

    walk(body)
    return targets


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


def _resolve_module_specifier(specifier: str, current_dir: str) -> list[str]:
    """Return candidate Module-node target names for an import specifier.

    Relative specifiers (`./foo`, `../bar/baz`) resolve to `{dir}/{path}.
    {ext}` candidates against the importing file's directory, across each of
    `.js/.jsx/.ts/.tsx`, plus `{dir}/{path}/index.{ext}` directory-import
    candidates - the same non-materializing-guess pattern the Python
    extractor uses for its own import targets (a candidate that doesn't
    match a real indexed Module node simply never produces an edge).

    Bare specifiers (`lodash`, `@scope/pkg`) get a single best-effort
    `node_modules/{name}` guess - no package.json/tsconfig 'paths' mapping is
    consulted (out of scope, see module docstring).
    """
    if specifier.startswith("."):
        base = _resolve_relative_base(current_dir, specifier)
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
    return [f"node_modules/{specifier}"]


def _extract_imports(root: Node, source: bytes, current_dir: str) -> list[tuple[str, list[str]]]:
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
                for target in _resolve_module_specifier(specifier, current_dir):
                    imports.append((target, names))
        elif node.type == "export_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                specifier = _string_value(source_node, source)
                names = _export_clause_names(node, source)
                for target in _resolve_module_specifier(specifier, current_dir):
                    imports.append((target, names))
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier" and _text(func, source) == "require":
                args = node.child_by_field_name("arguments")
                if args is not None and args.named_children:
                    first_arg = args.named_children[0]
                    if first_arg.type == "string":
                        specifier = _string_value(first_arg, source)
                        for target in _resolve_module_specifier(specifier, current_dir):
                            imports.append((target, [specifier]))

        for child in node.children:
            walk(child)

    walk(root)
    return imports


def extract_js_file(source_code: str, file_path: str, repo_id: str) -> ExtractionResult:
    """Parse a JS/TS/JSX/TSX file and extract nodes and relationships.

    Args:
        source_code: The file's source as a string.
        file_path: The Module node's identity - the file's path relative to
            the repo root, forward-slashed (e.g. 'src/services/api.ts').
            Also used to pick the Tree-sitter grammar, by extension.
        repo_id: Repository ID for scoping nodes.

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
    for target, names in _extract_imports(root, source_bytes, current_dir):
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

    def _emit_call(caller_name: str, caller_label: str, target_name: str, caller_class: str | None = None) -> None:
        properties = {"caller_class": caller_class} if caller_class else None
        result.relationships.append(
            GraphRelationship(
                from_label=caller_label,
                from_name=caller_name,
                rel_type="CALLS",
                to_label="Function",
                to_name=target_name,
                repo_id=repo_id,
                properties=properties,
            )
        )

    def _try_visit_definition(node: Node, parent_name: str | None, parent_label: str) -> bool:
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
            _visit_class(target, parent_name, parent_label, node)
            return True
        if target.type in ("function_declaration", "generator_function_declaration"):
            name_node = target.child_by_field_name("name")
            if name_node is not None:
                _visit_function(target, parent_name, parent_label, node, name_node)
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
                    and value_node is not None
                    and value_node.type in _FUNCTION_VALUE_TYPES
                )
                if is_named_fn:
                    _visit_variable_declarator(decl_node, parent_name, parent_label, node if first else None)
                    handled = True
                first = False
            return handled
        return False

    def _visit_statement(node: Node, parent_name: str | None, parent_label: str) -> None:
        """Visit a Module-top-level statement: definitions become Class/
        Function nodes; anything else is scanned for call expressions
        attributed to the enclosing Module (mirrors the Python extractor's
        module-level-statement handling, e.g. `setup();` at top level).
        """
        if _try_visit_definition(node, parent_name, parent_label):
            return
        if node.type == "export_statement":
            return  # declaration-less export - nothing left to scan for calls
        caller_class = parent_name if parent_label == "Class" else None
        for call_target in _extract_call_targets(node, source_bytes):
            _emit_call(parent_name if parent_name else file_path, parent_label, call_target, caller_class)

    def _visit_nested_defs(container: Node, parent_name: str, parent_label: str) -> None:
        """Visit a function body's direct statement children for nested
        named function/class/const-arrow definitions only - NOT a general
        call-scanning pass, since the caller (`_visit_function`) already ran
        `_extract_call_targets` over the whole body once, recursively
        (excluding nested scopes). Re-scanning non-definition statements
        here would double-emit CALLS edges for that body's top-level calls.
        Only direct children are checked (not statements nested inside an
        if/for/while block) - the same one-level limitation the Python
        extractor has for nested `def`s.
        """
        for node in container.named_children:
            _try_visit_definition(node, parent_name, parent_label)

    def _visit_variable_declarator(
        decl_node: Node, parent_name: str | None, parent_label: str, doc_anchor: Node | None
    ) -> None:
        name_node = decl_node.child_by_field_name("name")
        value_node = decl_node.child_by_field_name("value")
        if name_node is None or name_node.type != "identifier" or value_node is None:
            return
        if value_node.type in _FUNCTION_VALUE_TYPES:
            # doc_anchor is None for a non-first declarator in a multi-name
            # `const a = ..., b = ...` statement - no JSDoc attributed to it.
            _visit_function(value_node, parent_name, parent_label, doc_anchor or value_node, name_node)

    def _visit_class(node: Node, parent_name: str | None, parent_label: str, doc_anchor: Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        class_name = _text(name_node, source_bytes)
        base_classes = _extract_base_class_names(node, source_bytes)

        class_properties: dict = {
            "type": "class",
            "file": file_path,
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
        if body_node is None:
            return
        for member in body_node.named_children:
            if member.type == "method_definition":
                m_name_node = member.child_by_field_name("name")
                if m_name_node is not None and m_name_node.type == "property_identifier":
                    _visit_function(member, class_name, "Class", member, m_name_node)
            elif member.type in ("field_definition", "public_field_definition"):
                f_name_node = member.child_by_field_name("property")
                value_node = member.child_by_field_name("value")
                if (
                    f_name_node is not None
                    and f_name_node.type == "property_identifier"
                    and value_node is not None
                    and value_node.type in _FUNCTION_VALUE_TYPES
                ):
                    _visit_function(value_node, class_name, "Class", member, f_name_node)

    def _visit_function(
        node: Node, parent_name: str | None, parent_label: str, doc_anchor: Node, name_node: Node
    ) -> None:
        func_name = _text(name_node, source_bytes)

        func_properties: dict = {
            "type": "function",
            "file": file_path,
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
            )
        )

        body_node = node.child_by_field_name("body")
        if body_node is None:
            return

        caller_class = parent_name if parent_label == "Class" else None
        for call_target in _extract_call_targets(body_node, source_bytes):
            _emit_call(func_name, "Function", call_target, caller_class)

        # Nested function/class declarations - only meaningful when the body
        # is an actual statement block; a concise arrow body (`() => expr`)
        # can't syntactically contain a nested declaration statement. Uses
        # _visit_nested_defs (definitions only), NOT _visit_statement, since
        # the call scan above already covers every call in this body.
        if body_node.type == "statement_block":
            _visit_nested_defs(body_node, func_name, "Function")

    for node in root.named_children:
        _visit_statement(node, None, "Module")

    return result


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
