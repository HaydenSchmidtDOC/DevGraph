"""Fastify's full route form, `fastify.route({ method, url, handler })`
(jsts/extractor.py `_express_routes`): one route per method, with the
handler linked as the shorthand forms' is. Fixture code is fictional."""

import textwrap

from devgraph.indexer.jsts.extractor import extract_js_file


def _routes(code, file_path="src/server.js"):
    result = extract_js_file(textwrap.dedent(code), file_path, "repo")
    endpoints = sorted(n.name for n in result.nodes if n.label == "Endpoint")
    implements = sorted(
        (r.from_name, r.to_name) for r in result.relationships if r.rel_type == "IMPLEMENTS" and r.to_file
    )
    return endpoints, implements


def test_a_route_object_with_one_method_and_with_a_method_list():
    endpoints, implements = _routes("""\
        const fastify = require('fastify')();
        fastify.get('/f', ff);
        fastify.route({ method: 'POST', url: '/g', handler: gg });
        fastify.route({ method: ['GET', 'head'], url: '/h', schema: {}, handler: hh });
        function ff(request, reply) {}
        function gg(request, reply) {}
        function hh(request, reply) {}
    """)
    assert endpoints == ["GET /f", "GET /h", "HEAD /h", "POST /g"]
    assert implements == [("GET /f", "ff"), ("GET /h", "hh"), ("HEAD /h", "hh"), ("POST /g", "gg")]


def test_a_shorthand_or_inline_handler_and_an_imported_one():
    endpoints, implements = _routes("""\
        import Fastify from 'fastify';
        import { listOrders } from './orders';
        const app = Fastify();
        const handler = (request, reply) => {};
        app.route({ method: 'GET', url: '/a', handler });
        app.route({ method: 'GET', url: '/b', handler(request, reply) {} });
        app.route({ method: 'GET', url: '/c', handler: async (request, reply) => {} });
        app.route({ method: 'GET', url: '/orders', handler: listOrders });
    """)
    assert endpoints == ["GET /a", "GET /b", "GET /c", "GET /orders"]
    assert ("GET /a", "handler") in implements
    assert ("GET /orders", "listOrders") in implements


def test_a_dynamic_route_object_is_none():
    endpoints, _ = _routes("""\
        const fastify = require('fastify')();
        fastify.route({ method: verb, url: '/x', handler: h });
        fastify.route({ method: 'GET', url: PATH, handler: h });
        fastify.route(options);
        cache.route({ method: 'GET', url: '/y', handler: h });
        function h() {}
    """)
    assert endpoints == []
