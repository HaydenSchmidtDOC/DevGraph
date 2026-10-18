"""JS/TS object-literal methods, CommonJS exports and functions declared in
blocks are Functions with owners, their calls are their own, and calls to
them resolve (audit 7, item 6). Fixture code is fictional."""

import textwrap

from devgraph.indexer.jsts.extractor import extract_js_file


def _extract(source: str, path: str = "src/svc.ts"):
    return extract_js_file(textwrap.dedent(source), path, "repo")


def _symbols(result) -> set[tuple[str, str, str]]:
    return {(n.label, n.name, n.properties["owner"]) for n in result.nodes if n.label in ("Class", "Function")}


def _calls(result) -> set[tuple[str, str, str, str | None, str | None]]:
    return {
        (r.from_name, r.from_owner, r.to_name, r.to_file, r.to_owner)
        for r in result.relationships if r.rel_type == "CALLS"
    }


def test_object_literal_methods_are_owned_by_their_binding():
    result = _extract("""\
        import { repo } from './repo';

        export const userService = {
          async getUser(id) { return repo.find(id); },
          deleteUser: async (id) => repo.remove(id),
          audit: function () { return this.getUser(1); },
          admin: { purge() { return userService.getUser(1); } },
          label: makeLabel(),
        };

        function makeLabel() { return 'users'; }

        const cache = { clear() { return userService.deleteUser(1); } } as const;
        """)
    assert {
        ("Function", "getUser", "userService"), ("Function", "deleteUser", "userService"),
        ("Function", "audit", "userService"), ("Function", "purge", "userService.admin"),
        ("Function", "clear", "cache"),
    } <= _symbols(result)
    calls = _calls(result)
    # Their calls are their own; a call through the binding reaches its member.
    assert ("getUser", "userService", "find", "src/repo.ts", None) in calls
    assert ("deleteUser", "userService", "remove", "src/repo.ts", None) in calls
    assert ("purge", "userService.admin", "getUser", "src/svc.ts", "userService") in calls
    assert ("clear", "cache", "deleteUser", "src/svc.ts", "userService") in calls
    # A non-function property's calls are the module's.
    assert ("src/svc.ts", None, "makeLabel", "src/svc.ts", None) in calls
    contains = {(r.from_name, r.to_name, r.to_owner) for r in result.relationships if r.rel_type == "CONTAINS"}
    assert ("src/svc.ts", "getUser", "userService") in contains


def test_commonjs_exports_are_top_level_functions():
    result = _extract("""\
        const repo = require('./repo');

        exports.handler = async (event) => repo.save(event);
        module.exports.other = function other() { return exports.handler(1); };
        module.exports.tools = { tidy() { return module.exports.other(); } };
        """, "src/lambda.js")
    assert {
        ("Function", "handler", ""), ("Function", "other", ""), ("Function", "tidy", "tools"),
    } <= _symbols(result)
    calls = _calls(result)
    assert ("handler", "", "save", "src/repo.js", None) in calls
    assert ("other", "", "handler", "src/lambda.js", "") in calls
    assert ("tidy", "tools", "other", "src/lambda.js", "") in calls


def test_module_exports_object_members_are_top_level():
    result = _extract("""\
        function helper() { return 1; }
        module.exports = { run() { return helper(); }, stop: () => helper() };
        """, "src/jobs.js")
    assert {("Function", "run", ""), ("Function", "stop", "")} <= _symbols(result)
    assert ("run", "", "helper", "src/jobs.js", None) in _calls(result)


def test_functions_declared_in_blocks_are_their_scopes_own():
    result = _extract("""\
        import { helper } from './helper';

        if (flag) {
          function inIf() { return helper(); }
        } else {
          function inIf() { return 0; }
        }
        try {
          var fallback = function named() { return inIf(); };
        } catch (e) {}

        function outer() {
          for (const item of items) {
            function deep() { return fallback(); }
          }
          switch (mode) {
            case 1: { const local = { run() { return deep(); } }; }
          }
          return deep();
        }
        """)
    symbols = _symbols(result)
    assert {
        ("Function", "inIf", ""), ("Function", "fallback", ""), ("Function", "deep", "outer"),
        ("Function", "run", "outer.local"),
    } <= symbols
    # Both `inIf`s are one node.
    assert len([n for n in result.nodes if n.name == "inIf"]) == 1
    calls = _calls(result)
    assert ("inIf", "", "helper", "src/helper.ts", "") in calls
    assert ("fallback", "", "inIf", "src/svc.ts", None) in calls
    assert ("deep", "outer", "fallback", "src/svc.ts", None) in calls
    assert ("run", "outer.local", "deep", "src/svc.ts", None) in calls


def test_an_object_passed_as_an_argument_defines_nothing():
    result = _extract("""\
        export function setup() {
          register({ onReady() { return start(); } });
        }
        """)
    assert _symbols(result) == {("Function", "setup", "")}


def test_a_shorthand_member_links_as_before():
    result = _extract("""\
        function helper() { return 1; }
        const tools = { helper, run() { return 1; } };
        export function go() { return tools.helper(); }
        """)
    # `helper` is no member Function of `tools`: the call stays a by-name one.
    assert ("go", "", "helper", None, None) in _calls(result)


def test_an_imported_object_literals_member_is_reached_in_its_file():
    result = _extract("""\
        import { userService } from './svc';
        const handlers = require('./lambda');

        export function show(id) { return userService.getUser(id) && handlers.handler(id); }
        """, "src/page.ts")
    calls = _calls(result)
    assert ("show", "", "getUser", "src/svc.ts", None) in calls
    assert ("show", "", "handler", "src/lambda.js", None) in calls


def test_a_commonjs_export_never_merges_into_a_top_level_function_of_its_name():
    result = _extract("""\
        function create(x) { return x; }
        function remove(x) { return x; }
        module.exports = { create, remove, update: function (x) { return create(x); } };
        exports.extra = function () { return remove(1); };
        module.exports.create = function (y) { return y; };
        module.exports.check = { run() { return 1; } };
        """, "src/crud.js")
    nodes = [(n.name, n.properties["owner"], n.properties["start_line"]) for n in result.nodes if n.label == "Function"]
    # The export named like a top-level function is owned by `exports`: two nodes.
    assert sorted(nodes) == [
        ("create", "", 1), ("create", "exports", 5), ("extra", "", 4), ("remove", "", 2), ("run", "check", 6),
        ("update", "", 3),
    ]


def test_module_exports_receivers_reach_the_exports_owner():
    result = _extract("""\
        function create(x) { return x; }
        module.exports.create = function (y) { return y; };
        exports.helper = function () { return module.exports.create(1); };
        """, "src/crud.js")
    assert ("helper", "", "create", "src/crud.js", "exports") in _calls(result)
