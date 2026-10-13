"""The routes the non-Python extractors find (devgraph/indexer/apis/routes.py):
each framework's forms as Endpoints, their handler stubs and IMPLEMENTS edges,
on fictional fixture code."""

from devgraph.indexer.apis.routes import join_route
from devgraph.indexer.jsts.extractor import extract_js_file


def _endpoints(result) -> dict[str, str]:
    return {n.name: n.properties["framework"] for n in result.nodes if n.label == "Endpoint"}


def _implements(result) -> set[tuple]:
    """(endpoint, handler, to_file, to_owner) of every IMPLEMENTS edge."""
    return {
        (r.from_name, r.to_name, r.to_file, r.to_owner) for r in result.relationships if r.rel_type == "IMPLEMENTS"
    }


def _stubs(result) -> set[str]:
    return {n.name for n in result.nodes if n.label == "Function" and n.properties.get("type") == "handler"}


def test_join_route():
    assert join_route("/api/", "users/") == "/api/users"
    assert join_route("", "") == "/"
    assert join_route("api", "/{id}") == "/api/{id}"


EXPRESS = """\
import express from 'express';
import { listUsers as list } from './handlers';
import orders from '../controllers/orders';

const router = express.Router();
const app = express();

function showUser(req, res) { res.send('u'); }

router.get('/:id', showUser);
router.post('/', requireLogin, list);
router.put(`/${version}/x`, showUser);
router.delete('/a/' + suffix, showUser);
app.route('/books').get(orders.index).post((req, res) => res.end());
app.all('/health', function (req, res) {});
app.use('/api/users', router);

cache.get('/key', fallback);
axios.get('/users', { params });
$.get('/legacy', function () {});
"""


def test_express_routes_and_handlers():
    result = extract_js_file(EXPRESS, "src/routes/users.ts", "r")
    assert _endpoints(result) == {
        "GET /api/users/:id": "Express",
        "POST /api/users": "Express",
        "GET /books": "Express",
        "POST /books": "Express",
        "* /health": "Express",
    }
    implements = _implements(result)
    # A function of the file: its node there (top-level) and the stub.
    assert {("GET /api/users/:id", "showUser", "", None),
            ("GET /api/users/:id", "showUser", "src/routes/users.ts", "")} <= implements
    # An ES named import: the exported name, in the files the import can name.
    assert ("POST /api/users", "listUsers", "src/routes/handlers.ts", "") in implements
    assert ("POST /api/users", "listUsers", "", None) in implements
    # A member of a default import: any `index` in its files.
    assert ("GET /books", "index", "src/controllers/orders.ts", None) in implements
    # An inline handler implements nothing.
    assert not [e for e in implements if e[0] in ("POST /books", "* /health")]
    assert _stubs(result) == {"showUser", "listUsers", "index"}
    # Every route edge is the file's own.
    assert {r.origin for r in result.relationships if r.rel_type == "IMPLEMENTS"} == {"src/routes/users.ts"}


def test_express_router_mounted_elsewhere_keeps_its_own_paths():
    source = """\
const { Router } = require('express');
const orders = Router();
orders.get('/', listOrders);
async function listOrders(req, res) {}
module.exports = orders;
"""
    result = extract_js_file(source, "routes/orders.js", "r")
    assert _endpoints(result) == {"GET /": "Express"}


def test_express_needs_a_router_receiver_and_a_handler():
    source = """\
const client = makeClient();
client.get('/users', onDone);
app.get('/users');
app.get('title');
app.get('/users', 'text');
userRouter.patch('/users/:id', update);
const update = (req, res) => {};
"""
    result = extract_js_file(source, "app.js", "r")
    assert _endpoints(result) == {"PATCH /users/:id": "Express"}
    assert ("PATCH /users/:id", "update", "app.js", "") in _implements(result)


def test_koa_and_fastify_routes():
    source = """\
import Router from '@koa/router';
import Fastify from 'fastify';
const router = new Router();
const fastify = Fastify();
router.get('user', '/users/:id', show);
fastify.post('/items', { schema }, create);
function show(ctx) {}
function create(request, reply) {}
"""
    result = extract_js_file(source, "server.js", "r")
    assert _endpoints(result) == {"GET /users/:id": "Express", "POST /items": "Express"}
    assert _stubs(result) == {"show", "create"}
