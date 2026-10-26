"""JS/TS calls inside an anonymous callback (an arrow or function expression
passed as an argument or a JSX prop) are the enclosing named function's, the
way a Python lambda's, a Go closure's or a Java lambda's are; a call of the
callback's own parameter links nothing, and a named nested function keeps
its own calls."""

import textwrap

from devgraph.indexer.jsts.extractor import extract_js_file


def callees(source, caller, file_path="src/app.tsx"):
    result = extract_js_file(textwrap.dedent(source), file_path, "repo")
    return {r.to_name for r in result.relationships if r.rel_type == "CALLS" and r.from_name == caller}


def test_a_use_effect_body_and_its_then_are_the_components():
    source = """
        import { useEffect, useState } from 'react';
        import { fetchOrder } from './api';

        export function OrderView({ id }) {
          const [order, setOrder] = useState(null);
          useEffect(() => { fetchOrder(id).then((o) => track(o)); }, [id]);
          return null;
        }
        function track(o) {}
    """
    assert {"fetchOrder", "track"} <= callees(source, "OrderView")


def test_an_event_handler_prop_is_the_components():
    source = """
        import { save } from './api';
        export function Form() {
          return <button onClick={() => save()} onBlur={function () { audit(); }}>Save</button>;
        }
        function audit() {}
    """
    assert {"save", "audit"} <= callees(source, "Form")


def test_a_for_each_callback_is_the_enclosing_functions():
    source = """
        export function outer(xs: number[]) { xs.forEach((x) => targetTs(x)); }
        function targetTs(x: number) {}
    """
    assert "targetTs" in callees(source, "outer", "src/app.ts")


def test_a_callbacks_parameter_shadows_a_function_of_its_name():
    source = """
        export function run(items) { items.map((fn) => fn()); items.forEach(({ cb }) => cb()); }
        function fn() {}
        function cb() {}
    """
    got = callees(source, "run", "src/app.ts")
    assert "fn" not in got and "cb" not in got


def test_a_callbacks_own_local_shadows_too():
    source = """
        export function run(items) { items.forEach(() => { const helper = pick(); helper(); }); }
        function helper() {}
        function pick() {}
    """
    got = callees(source, "run", "src/app.ts")
    assert "pick" in got and "helper" not in got


def test_a_named_nested_function_keeps_its_own_calls():
    source = """
        export function outer() {
          const inner = () => { only(); };
          [1].map(() => inner());
        }
        function only() {}
    """
    assert "only" not in callees(source, "outer", "src/app.ts")
    assert "inner" in callees(source, "outer", "src/app.ts")
    assert "only" in callees(source, "inner", "src/app.ts")


def test_an_inline_express_middleware_body_is_the_modules():
    source = """
        const express = require('express');
        const { loadUser } = require('./users');
        const app = express();
        app.get('/users/:id', async (req, res) => { res.json(await loadUser(req.params.id)); });
    """
    assert "loadUser" in callees(source, "src/server.js", "src/server.js")



def test_this_in_a_function_expression_callback_is_not_the_class():
    """An arrow keeps the method's `this`; a `function` expression binds
    its own, so `this.persist()` there is not pinned to the class."""
    arrow = """
        export class Cart {
          persist() {}
          flush(items) { items.forEach(() => this.persist()); }
        }
    """
    plain = arrow.replace("() => this.persist()", "function () { this.persist(); }")

    def pins(source):
        result = extract_js_file(textwrap.dedent(source), "src/cart.ts", "repo")
        return {
            (r.to_file, r.properties["confidence"]) for r in result.relationships
            if r.rel_type == "CALLS" and r.from_name == "flush" and r.to_name == "persist"
        }

    assert ("src/cart.ts", "resolved") in pins(arrow)
    assert ("src/cart.ts", "resolved") not in pins(plain)


def test_a_method_of_an_unnamed_object_is_the_enclosing_functions():
    """`configure({ onDone() { ... } })` and a Vue options object's methods
    have no Function of their own: their calls are the function's (or the
    Module's) holding the object; a defined object's methods stay their own."""
    source = """
        export function outer() {
          configure({ onDone(result) { t5(); result(); }, get size() { return t6(); } });
        }
        export default { methods: { save() { t7(); } } };
        const service = { run() { t8(); } };
        function t5() {}
        function t6() {}
        function t7() {}
        function t8() {}
        function result() {}
    """
    got = callees(source, "outer", "src/app.ts")
    assert {"t5", "t6"} <= got and "result" not in got
    assert "t7" in callees(source, "src/app.ts", "src/app.ts")
    assert "t8" in callees(source, "run", "src/app.ts")
    assert "t8" not in callees(source, "src/app.ts", "src/app.ts")
